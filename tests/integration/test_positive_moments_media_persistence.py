"""
The Positive Moments media platform against the isolated test database.

Real repositories, real SQL, real compare-and-set transitions and claims.
Stubbed only where a test must never reach the outside world: the model, the
render provider (no Creatomate credits), SharePoint/Graph, and the pipeline
resolver (whose own suite covers it).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.config.settings import Settings
from app.db.repositories.media_platform import MediaPlatformRepository
from app.db.repositories.positive_moments import PositiveMomentRepository
from app.media import access
from app.media.dashboard import dashboard, lecture_media
from app.media.delivery import DeliveredItem, DeliveryError, SourceFile
from app.media.moments_media import MediaSettings, PositiveMomentMediaService
from app.media.render.base import SUCCEEDED, RenderProviderError, RenderStatus
from app.media.runner import MediaJobWorker, MediaRunProcessor, MediaRunner
from app.media.webhook import handle_creatomate_webhook, webhook_token
from app.positive_moments import policy as p
from app.positive_moments.service import PositiveMomentAnalyzer
from tests.integration.seeding import (
    DAY,
    TRAINER,
    insert,
    seed_lecture,
    seed_legacy_row,
    seed_transcript_document,
)

UTC = timezone.utc
LEARNER = "Synthetic Learner Amy"
TEMP_URL = "https://kbc.sharepoint.com/_layouts/15/download.aspx?tempauth=SECRET-TEMP-URL"
API_KEY = "ck_live_SECRET_API_KEY"
SECRET = "hmac-secret"


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        assert "test" in name, "refusing: not the isolated test database"
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# a seeded lecture: canonical cues, a linked recording, Graph metadata
# ---------------------------------------------------------------------------

CUES = [
    (0, 20_000, TRAINER, "Welcome, today we cover the earned value framework."),
    (30_000, 50_000, TRAINER, "Does that cost performance index make sense to everyone?"),
    (60_000, 75_000, LEARNER, "I was confused before but that makes much more sense now."),
    (80_000, 95_000, TRAINER, "Great, let us look at schedule variance next."),
    (120_000, 140_000, LEARNER, "This framework would really help in my role at work."),
    (150_000, 160_000, TRAINER, "Thanks everyone, see you next week."),
]
RECORDING_WINDOW = 3600.0


def seed(db, *, recording=True, coded_graph=True, file_duration=RECORDING_WINDOW,
         legacy_clips=None, completeness=None):
    lecture = seed_lecture(db)
    created = lecture["start"]
    document = seed_transcript_document(db, lecture, cues=CUES)
    db.execute("""UPDATE public.lecture_transcript_selection_parts
                     SET part_offset_ms = 0, provider_created_at = %s, provider_end_at = %s
                   WHERE selection_id = %s""",
               (created, created + timedelta(seconds=RECORDING_WINDOW), lecture["selection_id"]))
    session_id = lecture["transcript_id"]
    stamp = created - timedelta(seconds=40)
    file_name = f"{lecture['subject']}-{stamp:%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4"
    extra = {}
    if recording:
        extra = dict(recording_url="https://kbc.sharepoint.com/:v:/s/cohort/org-link",
                     recording_drive_id="drive-src", recording_item_id="item-src",
                     recording_filename=file_name,
                     recording_link_status="organization_view_link_created_exact_match")
    seed_legacy_row(db, session_id, meeting_id=lecture["meeting_id"], day=DAY,
                    subject=lecture["subject"], trainer="Dr Synthetic",
                    **({"clips_analysis_completeness": completeness} if completeness else {}),
                    **({"recording_url": None} if not recording else extra))
    if legacy_clips:
        db.execute("UPDATE public.qa_doctors_sessions SET positive_clips = %s::jsonb "
                   "WHERE session_id = %s", (json.dumps(legacy_clips), session_id))
    if recording and coded_graph:
        insert(db, "lecture_recording_links", lecture_id=lecture["lecture_id"],
               legacy_session_id=session_id, link_version="recording_link_v1",
               status="WRITTEN", stage_state="COMPLETE",
               reason="organization_view_link_created_exact_match",
               recording_item_id="item-src", recording_drive_id="drive-src",
               recording_url_written=True, attempt_count=1,
               metadata=({
                   "resolution_policy": "recording_match_v2_full_recording_resolution",
                   "graph": {"recordings": [{
                       "recording_id": "rec-1", "created_at": created.isoformat(),
                       "end_at": (created + timedelta(seconds=RECORDING_WINDOW)).isoformat(),
                       "content_correlation_id": None}]},
                   "resolution": {"rule": "full_recording_selected_over_short_fragment",
                                  "selected": {"duration_seconds": file_duration,
                                               "duration_source": "video_facet"}}}))
    return lecture, document, session_id


class StubResolver:
    def __init__(self, db, *, recording_state="COMPLETE", executable=None, reason=None):
        self.db, self.recording_state = db, recording_state
        self.executable, self.reason = executable, reason

    def for_lecture(self, connection, lecture_id):
        document = connection.execute(
            "SELECT document_id FROM public.lecture_transcript_documents WHERE lecture_id = %s",
            (lecture_id,)).fetchone()
        session = connection.execute(
            "SELECT q.session_id FROM public.lecture_sessions l JOIN public.qa_doctors_sessions q"
            " ON q.meeting_id = l.meeting_id WHERE l.lecture_id = %s", (lecture_id,)).fetchone()
        return {"stages": {
            "CANONICAL_CUES": {"state": "COMPLETE", "document_id": str(document[0])},
            "RECORDING_LINK": {"state": self.recording_state, "reason": self.reason,
                               "action": "MANUAL_REVIEW_REQUIRED"
                               if self.recording_state == "REVIEW_REQUIRED" else None,
                               "legacy_session_id": session[0]}},
            "executable_stage": self.executable}


class ScriptedModel:
    def __init__(self, selector, verifier):
        self.selector, self.verifier, self.calls = selector, verifier, 0

    def complete_schema(self, *, schema_name, **kwargs):
        self.calls += 1
        output = self.selector if schema_name == "positive_moment_candidates" else self.verifier
        return {"output": output, "provider": "openai", "model_requested": "gpt-test",
                "usage": {}, "attempts": 1}


def model_finding_one():
    return ScriptedModel(
        {"candidates": [{"start_cue": 2, "end_cue": 3, "positive_speakers": [LEARNER],
                         "category": "content", "reason": "clarity", "confidence": 0.8},
                        {"start_cue": 6, "end_cue": 6, "positive_speakers": [TRAINER],
                         "category": "trainer", "reason": "x", "confidence": 0.8}]},
        {"verdicts": [{"candidate_id": "m1", "accept": True, "category": "learning_experience",
                       "training_target": "learning_experience",
                       "positive_speakers": [LEARNER], "rejection_code": None,
                       "confidence": 0.92}]})


def service(db, *, model=None, resolver=None, sharepoint=None):
    moments = PositiveMomentRepository()
    return PositiveMomentMediaService(
        resolver=resolver or StubResolver(db), moments_repository=moments,
        media_repository=MediaPlatformRepository(),
        analyzer=PositiveMomentAnalyzer(repository=moments, model=model or model_finding_one()),
        sharepoint=sharepoint, settings=MediaSettings())


class FakeProvider:
    name = "creatomate"

    def __init__(self):
        self.submitted, self.status_calls = [], 0
        self.status = "rendering"

    def submit(self, request):
        self.submitted.append(request)
        return RenderStatus(f"render-{uuid.uuid4().hex}", "QUEUED", "planned")

    def get_status(self, render_id):
        self.status_calls += 1
        if self.status == "succeeded":
            duration = self.submitted[-1].trim_duration_seconds if self.submitted else 1.0
            return RenderStatus(render_id, SUCCEEDED, "succeeded",
                                "https://cdn.creatomate.com/renders/x.mp4", duration, 2048,
                                1280, 720)
        return RenderStatus(render_id, "RENDERING", "rendering")


class FakeSharePoint:
    def __init__(self, tmp_path, *, fail_upload=None, source_errors=()):
        self.tmp_path, self.fail_upload = tmp_path, fail_upload
        self.uploads, self.temp_files = [], []
        self.source_errors = list(source_errors)
        self.source_calls = 0

    def source_file(self, drive_id, item_id):
        self.source_calls += 1
        if self.source_errors:
            raise self.source_errors.pop(0)
        # A fresh temporary URL on every call, as Graph issues them.
        return SourceFile(item_id, "x.mp4", 1, RECORDING_WINDOW,
                          f"{TEMP_URL}&fresh={self.source_calls}")

    def item_facts(self, drive_id, item_id):
        return {"name": None, "size_bytes": 1, "duration_seconds": RECORDING_WINDOW}

    def download_to_temp(self, url, *, expected_size=None, directory=None):
        path = self.tmp_path / f"render-{uuid.uuid4().hex}.mp4"
        path.write_bytes(b"x" * (expected_size or 10))
        self.temp_files.append(path)
        return str(path)

    def upload(self, path, *, drive_id, folder_item_id, filename):
        if self.fail_upload:
            raise self.fail_upload
        self.uploads.append(filename)
        return DeliveredItem(item_id=f"out-{len(self.uploads)}", drive_id=drive_id,
                             web_url=f"https://kbc.sharepoint.com/sites/clips/{filename}",
                             size_bytes=os.path.getsize(path), name=filename)


def worker(provider, sharepoint):
    return MediaJobWorker(media_repository=MediaPlatformRepository(),
                          moments_repository=PositiveMomentRepository(), provider=provider,
                          sharepoint=sharepoint, destination_drive_id="drive-dest",
                          destination_folder_item_id="folder-dest")


def poll_due(db):
    db.execute("UPDATE public.positive_moment_media_jobs SET next_poll_at = now() "
               "WHERE status = 'RENDERING'")


def claim_and_step(db, provider, sharepoint, runner="test-runner"):
    poll_due(db)
    job = MediaPlatformRepository().claim_job(db, runner)
    assert job is not None, "nothing claimable"
    return worker(provider, sharepoint).step(db, job)


def everything_as_text(db):
    tables = ["positive_moment_analyses", "positive_moments", "positive_moment_media_jobs",
              "positive_moment_media_events", "positive_moment_media_assets",
              "positive_moment_runs", "positive_moment_run_items",
              "positive_moment_lecture_states"]
    return "".join(str(db.execute(f"SELECT coalesce(json_agg(t)::text, '') FROM public.{t} t"
                                  ).fetchone()[0]) for t in tables)


# ---------------------------------------------------------------------------
# analysis persistence
# ---------------------------------------------------------------------------

def test_analysis_persists_verified_moments_with_lineage_and_is_idempotent(db):
    lecture, document, session_id = seed(db)
    model = model_finding_one()
    analyzer = PositiveMomentAnalyzer(repository=PositiveMomentRepository(), model=model)
    first = analyzer.analyze(db, lecture_id=lecture["lecture_id"],
                             document_id=document["document_id"],
                             legacy_session_id=session_id, allow_model=True)
    again = analyzer.analyze(db, lecture_id=lecture["lecture_id"],
                             document_id=document["document_id"],
                             legacy_session_id=session_id, allow_model=True)
    assert first.status == "MOMENTS_FOUND" and again.reused is True and model.calls == 2
    [row] = PositiveMomentRepository().moments(db, first.analysis["analysis_id"])
    assert (row["start_cue"], row["end_cue"]) == (2, 3)
    assert row["positive_speakers"] == [LEARNER] and TRAINER in row["all_speakers"]
    assert row["exact_quote"].endswith(CUES[2][3])
    assert row["category"] == "learning_experience" and row["verifier_verdict"] == "ACCEPTED"
    analysis = first.analysis
    assert analysis["document_id"] == document["document_id"]
    assert analysis["analysis_policy_version"] == p.ANALYSIS_POLICY_VERSION
    assert analysis["rejection_summary"] == {p.TRAINER_AS_POSITIVE_SPEAKER: 1}
    count = db.execute("SELECT count(*) FROM public.positive_moment_analyses "
                       "WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0]
    assert count == 1


def test_no_positive_moments_persists(db):
    lecture, document, session_id = seed(db)
    outcome = PositiveMomentAnalyzer(
        repository=PositiveMomentRepository(),
        model=ScriptedModel({"candidates": []}, {"verdicts": []})).analyze(
        db, lecture_id=lecture["lecture_id"], document_id=document["document_id"],
        legacy_session_id=session_id, allow_model=True)
    assert outcome.analysis["status"] == "NO_POSITIVE_MOMENTS"
    assert outcome.analysis["accepted_count"] == 0


def test_a_provable_legacy_v5_result_is_imported_once(db):
    clip = {"start": "00:01:00.000", "end": "00:01:15.000",
            "positive_quote": CUES[2][3], "positive_speakers": [LEARNER],
            "category": "learning_experience", "start_cue": 17, "end_cue": 17}
    lecture, document, session_id = seed(db, legacy_clips=[clip],
                                         completeness=p.LEGACY_V5_COMPLETENESS)
    model = model_finding_one()
    analyzer = PositiveMomentAnalyzer(repository=PositiveMomentRepository(), model=model)
    for _ in range(2):
        outcome = analyzer.analyze(db, lecture_id=lecture["lecture_id"],
                                   document_id=document["document_id"],
                                   legacy_session_id=session_id, allow_model=True)
    assert model.calls == 0 and outcome.source == p.SOURCE_LEGACY_V5
    rows = db.execute("SELECT source, start_cue FROM public.positive_moments "
                      "WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall()
    assert rows == [("legacy_v5_import", 3)]


# ---------------------------------------------------------------------------
# planning and the lecture snapshot
# ---------------------------------------------------------------------------

def test_a_resolved_recording_and_proven_alignment_plan_a_ready_clip(db):
    lecture, _, _ = seed(db)
    svc = service(db)
    outcome = svc.process(db, lecture["lecture_id"], analyze=True, allow_model=True,
                          persist_jobs=True)
    MediaPlatformRepository().save_state(db, outcome.state)
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    assert job["status"] == "READY_TO_RENDER"
    assert job["alignment_status"] == "ALIGNED"
    assert float(job["media_offset_seconds"]) == 0.0
    assert (float(job["actual_media_start_seconds"]), float(job["actual_media_end_seconds"])) \
        == (0.0, 135.0)      # evidence 30..75 s, 60 s before clamps to 0
    assert float(job["padding_before_applied_seconds"]) == 30.0
    assert (job["source_drive_id"], job["source_item_id"]) == ("drive-src", "item-src")
    state = outcome.state
    assert (state["transcript_state"], state["recording_state"], state["analysis_state"]) == (
        "READY", "READY", "MOMENTS_FOUND")
    assert state["recording_rule"] == "full_recording_selected_over_short_fragment"
    again = svc.process(db, lecture["lecture_id"], analyze=True, allow_model=True,
                        persist_jobs=True)
    assert again.jobs_planned == 0
    assert len(MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])) == 1


def test_an_unresolved_recording_waits_and_never_reaches_the_provider(db, tmp_path):
    lecture, _, _ = seed(db, recording=False)
    resolver = StubResolver(db, recording_state="REVIEW_REQUIRED", reason="MULTIPART_RECORDING")
    outcome = service(db, resolver=resolver).process(
        db, lecture["lecture_id"], analyze=True, allow_model=True, persist_jobs=True)
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    assert job["status"] == "WAITING_FOR_RECORDING"
    assert outcome.state["recording_status"] == "MULTIPART_RECORDING"
    assert service(db).queue_render(db, lecture_id=lecture["lecture_id"]) == 0
    assert MediaPlatformRepository().claim_job(db, "r") is None


def test_an_unprovable_alignment_is_review_and_cannot_be_queued(db):
    lecture, _, _ = seed(db, file_duration=RECORDING_WINDOW - 900)
    service(db).process(db, lecture["lecture_id"], analyze=True, allow_model=True,
                        persist_jobs=True)
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    assert job["status"] == "REVIEW_REQUIRED_ALIGNMENT"
    assert job["error_code"] == "FILE_DURATION_CONTRADICTS_RECORDING"
    assert service(db).queue_render(db, lecture_id=lecture["lecture_id"]) == 0


def test_the_dashboard_counts_and_filters_from_persisted_state(db):
    lecture, _, _ = seed(db)
    outcome = service(db).process(db, lecture["lecture_id"], analyze=True, allow_model=True,
                                  persist_jobs=True)
    MediaPlatformRepository().save_state(db, outcome.state)
    board = dashboard(db, media_repository=MediaPlatformRepository(), date_from=DAY,
                      date_to=DAY)
    ours = next(r for r in board["lectures"] if r["lecture_id"] == str(lecture["lecture_id"]))
    assert {"moments_found", "ready_to_render", "recording_ready"} <= set(ours["filters"])
    counters = {c["filter"]: c["count"] for c in board["counters"]}
    assert counters["ready_to_render"] == sum(
        "ready_to_render" in r["filters"] for r in board["lectures"])
    assert board["cost_preview"]["is_estimate"] is True
    assert board["cost_preview"]["safe_clips_planned"] >= 1


# ---------------------------------------------------------------------------
# render -> webhook -> SharePoint -> asset
# ---------------------------------------------------------------------------

def planned(db, **kwargs):
    lecture, _, _ = seed(db, **kwargs)
    svc = service(db)
    svc.process(db, lecture["lecture_id"], analyze=True, allow_model=True, persist_jobs=True)
    return lecture, svc


def test_the_full_render_and_delivery_flow(db, tmp_path):
    lecture, svc = planned(db)
    assert svc.queue_render(db, lecture_id=lecture["lecture_id"]) == 1
    assert svc.queue_render(db, lecture_id=lecture["lecture_id"]) == 0      # idempotent
    provider, sharepoint = FakeProvider(), FakeSharePoint(tmp_path)

    assert claim_and_step(db, provider, sharepoint) == "RENDERING"
    [request] = provider.submitted
    assert request.source_url == f"{TEMP_URL}&fresh=1"
    assert (request.trim_start_seconds, request.trim_duration_seconds) == (0.0, 135.0)

    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    provider.status = "succeeded"
    payload = {"id": job["provider_render_id"], "status": "succeeded"}
    first = handle_creatomate_webhook(db, media_repository=MediaPlatformRepository(),
                                      provider=provider, secret=SECRET, job_id=job["job_id"],
                                      token=webhook_token(SECRET, job["job_id"]), payload=payload)
    duplicate = handle_creatomate_webhook(db, media_repository=MediaPlatformRepository(),
                                          provider=provider, secret=SECRET, job_id=job["job_id"],
                                          token=webhook_token(SECRET, job["job_id"]),
                                          payload=payload)
    assert (first["result"], duplicate["result"]) == ("UPLOAD_PENDING", "ALREADY_PROCESSED")

    assert claim_and_step(db, provider, sharepoint) == "COMPLETED"
    assert len(sharepoint.uploads) == 1
    assert all(not path.exists() for path in sharepoint.temp_files)     # temp file cleaned
    [asset] = MediaPlatformRepository().assets_for_lecture(db, lecture["lecture_id"])
    assert asset["destination_item_id"] == "out-1"
    assert asset["output_filename"].endswith(f"_{job['plan_fingerprint'][:8]}.mp4")

    detail = lecture_media(db, lecture_id=lecture["lecture_id"],
                           media_repository=MediaPlatformRepository(),
                           moments_repository=PositiveMomentRepository())
    [moment] = detail["moments"]
    assert moment["asset"]["output_filename"] == asset["output_filename"]
    assert moment["job"]["status"] == "COMPLETED"
    assert [s["stage"] for s in moment["timeline"] if s["done"]] == [
        "EVIDENCE_SELECTED", "VERIFIED", "RECORDING_RESOLVED", "ALIGNMENT_CALCULATED",
        "RENDER_SUBMITTED", "RENDER_COMPLETED", "UPLOADED", "COMPLETE"]
    assert "https://" not in json.dumps(detail, default=str)             # no URL in detail
    url, code = access.asset_link(db, asset["asset_id"])
    assert code == "OK" and url.startswith("https://kbc.sharepoint.com/")
    rec_url, rec_code = access.recording_link(db, lecture["lecture_id"])
    assert rec_code == "OK" and rec_url.endswith("org-link")

    # A completed plan is never rendered again.
    assert svc.queue_render(db, lecture_id=lecture["lecture_id"]) == 0
    svc.process(db, lecture["lecture_id"], analyze=True, allow_model=True, persist_jobs=True)
    assert MediaPlatformRepository().claim_job(db, "r") is None
    assert len(provider.submitted) == 1

    stored = everything_as_text(db)
    assert "SECRET-TEMP-URL" not in stored and API_KEY not in stored
    assert "cdn.creatomate.com" not in stored


def test_a_failed_transfer_is_retryable_with_backoff(db, tmp_path):
    lecture, svc = planned(db)
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    provider = FakeProvider()
    claim_and_step(db, provider, FakeSharePoint(tmp_path))
    provider.status = "succeeded"
    claim_and_step(db, provider, FakeSharePoint(tmp_path))       # poll -> UPLOAD_PENDING
    failing = FakeSharePoint(tmp_path, fail_upload=DeliveryError("UPLOAD_CHUNK_HTTP_503", "x"))
    assert claim_and_step(db, provider, failing) == "FAILED_RETRYABLE"
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    assert job["resume_stage"] == "UPLOAD" and job["next_retry_at"] > datetime.now(UTC)
    assert MediaPlatformRepository().claim_job(db, "r") is None           # not yet due
    db.execute("UPDATE public.positive_moment_media_jobs SET next_retry_at = now() "
               "WHERE job_id = %s", (job["job_id"],))
    assert claim_and_step(db, provider, failing) == "UPLOAD_PENDING"       # re-armed
    assert claim_and_step(db, provider, FakeSharePoint(tmp_path)) == "COMPLETED"
    assert len(provider.submitted) == 1                                  # never re-rendered


def test_a_submit_that_died_mid_call_is_never_silently_resubmitted(db, tmp_path):
    lecture, svc = planned(db)
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    db.execute("UPDATE public.positive_moment_media_jobs SET submitted_at = now() "
               "WHERE job_id = %s", (job["job_id"],))
    provider = FakeProvider()
    assert claim_and_step(db, provider, FakeSharePoint(tmp_path)) == "FAILED_FINAL"
    assert provider.submitted == []
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    assert job["error_code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert svc.retry_failed(db, lecture_id=lecture["lecture_id"]) == 1     # explicit only


# ---------------------------------------------------------------------------
# the source download URL (production failure 2026-09-27)
# ---------------------------------------------------------------------------

@pytest.fixture
def settle(db):
    """
    submit() commits by design (the double-spend guard), so these tests leave
    committed jobs behind. Retire them afterwards, or a later test's
    claim_job() would pick them up.
    """
    lectures = []
    yield lectures.append
    db.rollback()
    db.execute("UPDATE public.positive_moment_media_jobs SET status = 'SUPERSEDED', "
               "locked_at = NULL, locked_by = NULL WHERE lecture_id = ANY(%s) "
               "AND status NOT IN ('COMPLETED', 'SUPERSEDED')",
               ([str(lecture_id) for lecture_id in lectures],))
    db.commit()


def _only_job(db, lecture):
    [job] = MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])
    return job


def _due(db, job):
    db.execute("UPDATE public.positive_moment_media_jobs SET next_retry_at = now() "
               "WHERE job_id = %s", (job["job_id"],))


def test_a_job_that_failed_for_no_source_url_submits_exactly_once_on_retry(
        db, tmp_path, settle, caplog):
    """The production shape: FAILED_RETRYABLE at RENDER_SUBMIT, no render id."""
    caplog.set_level("DEBUG")
    lecture, svc = planned(db)
    settle(lecture["lecture_id"])
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    provider = FakeProvider()
    unavailable = DeliveryError("SOURCE_GRAPH_DOWNLOAD_URL_UNAVAILABLE", "no download URL")
    failing = FakeSharePoint(tmp_path, source_errors=[unavailable])
    assert claim_and_step(db, provider, failing) == "FAILED_RETRYABLE"
    job = _only_job(db, lecture)
    assert (job["error_code"], job["resume_stage"], job["provider_render_id"],
            job["submitted_at"]) == ("SOURCE_GRAPH_DOWNLOAD_URL_UNAVAILABLE", "SUBMIT", None, None)
    assert provider.submitted == []                              # no Creatomate call

    # The operator's Retry re-arms THIS job; no second job, no second plan.
    assert svc.retry_failed(db, lecture_id=lecture["lecture_id"]) == 1
    _due(db, job)      # the test transaction's now() predates the retry stamp
    sharepoint = FakeSharePoint(tmp_path)
    assert claim_and_step(db, provider, sharepoint) == "QUEUED"               # re-armed
    assert claim_and_step(db, provider, sharepoint) == "RENDERING"            # submitted
    assert len(provider.submitted) == 1
    assert provider.submitted[0].source_url == f"{TEMP_URL}&fresh=1"         # fresh, in memory
    job = _only_job(db, lecture)
    assert job["provider_render_id"] and job["error_code"] is None
    assert len(MediaPlatformRepository().jobs_for_lecture(db, lecture["lecture_id"])) == 1

    stored = everything_as_text(db)
    assert "SECRET-TEMP-URL" not in stored and "tempauth" not in stored
    assert "SECRET-TEMP-URL" not in caplog.text and "tempauth" not in caplog.text


def test_every_submission_reads_a_new_source_url(db, tmp_path, settle):
    lecture, svc = planned(db)
    settle(lecture["lecture_id"])
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    provider = FakeProvider()
    provider.submit = lambda request: (_ for _ in ()).throw(
        RenderProviderError("CREATOMATE_UNREACHABLE", "unreachable", retryable=True))
    sharepoint = FakeSharePoint(tmp_path)
    assert claim_and_step(db, provider, sharepoint) == "FAILED_RETRYABLE"
    _due(db, _only_job(db, lecture))
    ok = FakeProvider()
    assert claim_and_step(db, ok, sharepoint) == "QUEUED"
    assert claim_and_step(db, ok, sharepoint) == "RENDERING"
    assert sharepoint.source_calls == 2
    assert ok.submitted[0].source_url == f"{TEMP_URL}&fresh=2"    # not the first attempt's


def test_a_retry_of_a_job_with_a_render_id_reconciles_instead_of_resubmitting(
        db, tmp_path, settle):
    lecture, svc = planned(db)
    settle(lecture["lecture_id"])
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    provider, sharepoint = FakeProvider(), FakeSharePoint(tmp_path)
    assert claim_and_step(db, provider, sharepoint) == "RENDERING"
    job = _only_job(db, lecture)
    # A poll failure leaves the job FAILED_RETRYABLE, resuming at POLL, with its render id.
    db.execute("UPDATE public.positive_moment_media_jobs SET status = 'FAILED_RETRYABLE', "
               "resume_stage = 'POLL' WHERE job_id = %s", (job["job_id"],))
    assert svc.retry_failed(db, lecture_id=lecture["lecture_id"]) == 1
    _due(db, job)      # the test transaction's now() predates the retry stamp
    assert claim_and_step(db, provider, sharepoint) == "RENDERING"         # re-armed to poll
    assert claim_and_step(db, provider, sharepoint) == "RENDERING"         # polled
    # A QUEUED job that already holds a render id is reconciled, not resubmitted.
    db.execute("UPDATE public.positive_moment_media_jobs SET status = 'QUEUED' "
               "WHERE job_id = %s", (job["job_id"],))
    assert claim_and_step(db, provider, sharepoint) == "RENDERING"
    assert _only_job(db, lecture)["provider_render_id"] == job["provider_render_id"]
    assert len(provider.submitted) == 1                                    # never twice
    assert sharepoint.source_calls == 1 and provider.status_calls >= 1


@pytest.mark.parametrize("code, operator_retry", [
    ("SOURCE_GRAPH_PERMISSION_DENIED", 1),          # recoverable once access is granted
    ("SOURCE_GRAPH_ITEM_NOT_FOUND", 0),             # the plan's source is gone: re-plan
    ("SOURCE_GRAPH_ITEM_MISMATCH", 0),
])
def test_permanent_source_failures_are_final_and_never_reach_creatomate(
        db, tmp_path, settle, code, operator_retry):
    lecture, svc = planned(db)
    settle(lecture["lecture_id"])
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    provider = FakeProvider()
    failing = FakeSharePoint(tmp_path,
                             source_errors=[DeliveryError(code, "refused", retryable=False)])
    assert claim_and_step(db, provider, failing) == "FAILED_FINAL"
    job = _only_job(db, lecture)
    assert (job["error_code"], job["attempt_count"]) == (code, 1)          # no timed retries
    assert provider.submitted == []
    assert MediaPlatformRepository().claim_job(db, "r") is None
    assert svc.retry_failed(db, lecture_id=lecture["lecture_id"]) == operator_retry


def test_a_job_claim_is_atomic_across_connections(db):
    """Two runners, committed data, one queued job: exactly one claim wins."""
    url = Settings.from_environment().database_url
    lecture_id, analysis_id, moment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(url) as setup:
        insert(setup, "lecture_sessions", lecture_id=lecture_id,
               calendar_user_upn="calendar@example.invalid",
               calendar_event_id=f"evt-{lecture_id}", join_url="https://example.invalid/x",
               subject="Atomic Claim", normalized_subject="atomic claim", module="m",
               scheduled_start=datetime(2031, 3, 4, 9, tzinfo=UTC),
               scheduled_end=datetime(2031, 3, 4, 11, tzinfo=UTC), session_date=DAY,
               calendar_mapping_status="RESOLVED", group_match_status="MATCHED",
               discovery_status="READY", meeting_id=f"atomic-{lecture_id}")
        insert(setup, "positive_moment_analyses", analysis_id=analysis_id, lecture_id=lecture_id,
               document_id=uuid.uuid4(), document_source_fingerprint="d",
               transcript_fingerprint="t", analysis_policy_version=p.ANALYSIS_POLICY_VERSION,
               input_fingerprint=f"i-{analysis_id}", source="coded_ai", status="MOMENTS_FOUND")
        insert(setup, "positive_moments", moment_id=moment_id, analysis_id=analysis_id,
               lecture_id=lecture_id, document_id=uuid.uuid4(), transcript_fingerprint="t",
               analysis_policy_version=p.ANALYSIS_POLICY_VERSION, moment_fingerprint="m",
               moment_index=1, start_cue=1, end_cue=1, evidence_start_ms=0,
               evidence_end_ms=1000, conversation_type="learner_statement",
               category="content", exact_quote="q", positive_quote="q",
               verifier_verdict="ACCEPTED", source="coded_ai")
        MediaPlatformRepository().insert_job(setup, {
            "moment_id": moment_id, "lecture_id": lecture_id, "provider": "creatomate",
            "plan_fingerprint": f"atomic-{moment_id}", "evidence_start_ms": 0,
            "evidence_end_ms": 1000, "alignment_status": "ALIGNED", "status": "QUEUED"})
        setup.commit()
    try:
        with psycopg.connect(url) as a, psycopg.connect(url) as b:
            first = MediaPlatformRepository().claim_job(a, "runner-a")
            second = MediaPlatformRepository().claim_job(b, "runner-b")    # row locked by a
            a.commit()
            third = MediaPlatformRepository().claim_job(b, "runner-b")     # lease held by a
            b.commit()
            mine = lambda job: job and str(job["lecture_id"]) == str(lecture_id)  # noqa: E731
            assert mine(first) and not mine(second) and not mine(third)
    finally:
        with psycopg.connect(url) as cleanup:
            cleanup.execute("DELETE FROM public.lecture_sessions WHERE lecture_id = %s",
                            (lecture_id,))
            cleanup.commit()


# ---------------------------------------------------------------------------
# batch runs survive restarts
# ---------------------------------------------------------------------------

def test_a_batch_run_resumes_after_a_restart_without_redoing_work(db, tmp_path):
    first, _, _ = seed(db)
    second, _, _ = seed(db)
    media = MediaPlatformRepository()
    run = media.create_run(db, requested_from=DAY, requested_to=DAY, mode="ANALYZE",
                           created_by="test")
    model = model_finding_one()
    processor = MediaRunProcessor(media_repository=media,
                                  media_service=service(db, model=model))
    claimed = media.claim_run(db, "runner-1", "v")
    assert claimed["run_id"] == run["run_id"]
    assert processor.step(db, claimed, runner="runner-1") is True
    # runner-1 dies. Its heartbeat goes stale; a new runner reclaims the run.
    assert media.claim_run(db, "runner-2", "v") is None                # not stale yet
    db.execute("UPDATE public.positive_moment_runs SET heartbeat_at = now() - interval '1 hour'"
               " WHERE run_id = %s", (run["run_id"],))
    reclaimed = media.claim_run(db, "runner-2", "v")
    assert reclaimed["run_id"] == run["run_id"]
    calls_before = model.calls
    while processor.step(db, reclaimed, runner="runner-2"):
        pass
    done = media.run_items_done(db, run["run_id"])
    assert {str(first["lecture_id"]), str(second["lecture_id"])} <= done
    assert media.run(db, run["run_id"])["status"] == "COMPLETED"
    analyses = db.execute("SELECT count(*) FROM public.positive_moment_analyses "
                          "WHERE lecture_id = ANY(%s::uuid[])",
                          ([str(first["lecture_id"]), str(second["lecture_id"])],)).fetchone()[0]
    assert analyses == 2
    assert model.calls - calls_before <= 2 * (len(done) - 1)    # nothing analysed twice


def test_analysis_runs_never_submit_renders(db, tmp_path):
    lecture, _, _ = seed(db)
    media = MediaPlatformRepository()
    media.create_run(db, requested_from=DAY, requested_to=DAY, mode="ANALYZE",
                     lecture_id=lecture["lecture_id"], created_by="t")
    provider = FakeProvider()
    runner = MediaRunner(
        connection_factory=lambda: _Borrowed(db),
        worker_factory=lambda: worker(provider, FakeSharePoint(tmp_path)),
        run_processor_factory=lambda: MediaRunProcessor(media_repository=media,
                                                        media_service=service(db)),
        media_repository=media, runner_id="r")
    for _ in range(4):
        runner.once()
    assert provider.submitted == []
    [job] = media.jobs_for_lecture(db, lecture["lecture_id"])
    assert job["status"] == "READY_TO_RENDER"


class _Borrowed:
    """The test transaction, lent to the runner without closing it."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return _NoCommit(self.connection)

    def __exit__(self, *exc):
        return False


class _NoCommit:
    def __init__(self, connection):
        self._c = connection

    def commit(self):
        pass

    def rollback(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        if name == "_c":
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# legacy ownership
# ---------------------------------------------------------------------------

def test_media_never_modifies_legacy_qa_or_n8n_media_tables(db, tmp_path):
    lecture, _, session_id = seed(db)

    def legacy():
        return db.execute("""
            SELECT (SELECT md5(coalesce(string_agg(q::text, '|' ORDER BY session_id), ''))
                      FROM public.qa_doctors_sessions q),
                   (SELECT count(*) FROM public.qa_media_jobs),
                   (SELECT count(*) FROM public.qa_positive_clip_assets),
                   (SELECT md5(coalesce(string_agg(r::text, '|'), ''))
                      FROM public.lecture_recording_links r)""").fetchone()

    before = legacy()
    svc = service(db)
    svc.process(db, lecture["lecture_id"], analyze=True, allow_model=True, persist_jobs=True)
    svc.queue_render(db, lecture_id=lecture["lecture_id"])
    provider = FakeProvider()
    claim_and_step(db, provider, FakeSharePoint(tmp_path))
    provider.status = "succeeded"
    claim_and_step(db, provider, FakeSharePoint(tmp_path))
    claim_and_step(db, provider, FakeSharePoint(tmp_path))
    assert legacy() == before


def test_the_migration_refuses_unknown_statuses(db):
    lecture, _, _ = seed(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute("INSERT INTO public.positive_moment_runs (run_id, requested_from, "
                       "requested_to, mode, status) VALUES (%s, %s, %s, 'RENDER', 'INVENTED')",
                       (uuid.uuid4(), DAY, DAY))
