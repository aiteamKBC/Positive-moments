"""
P0-A: proposed migrations 024 (amended), 025 and 026 against the isolated test
database, applied INSIDE the test transaction and rolled back afterwards.
Covers the constraints, the LMS-match repository and the publisher queue.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.bunny.identity import build_title, recording_identity, video_record_id
from app.config.settings import Settings
from app.db.repositories.lms_matches import LmsMatchRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.lms import coverage
from app.lms.matching import (
    CONFLICT, NO_LMS_SESSION, OUT_OF_LMS_SCOPE, STALE, VERIFIED, MatchConfig, match_lecture,
)
from tests.integration.publisher_db import (
    MIGRATIONS, NEW_TABLES, ROLLBACKS, applied, body, lecture_row, reset_proposed_migrations,
)
from tests.unit.lms_fakes import START, channel_url, lecture, occurrence, snapshot
from tools.integration_db_guard import assert_isolated

LIBRARY = 786045
CONFIG = MatchConfig.from_text("Final Test")
repo = LmsMatchRepository()
queue = PublisherQueueRepository()


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    reset_proposed_migrations(url)              # a killed earlier run must not fail this one
    connection = psycopg.connect(url)
    try:
        assert "test" in connection.execute("SELECT current_database()").fetchone()[0]
        assert_isolated(connection)               # before the DDL: the live check rolls back once
        assert applied(connection) == [], "P0-A migrations must not already be applied to the test database"
        for path in MIGRATIONS:
            connection.execute(body(path))
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _fails(connection, sql, args=(), error=psycopg.errors.IntegrityError):
    with pytest.raises(error):
        with connection.transaction():
            connection.execute(sql, args)


def _lecture(db, start=START):
    lecture_id = lecture_row(db, start)
    return lecture_id, lecture(lecture_id=lecture_id, start=start)


def _record(db, facts, snap):
    return repo.record(db, match_lecture(facts, snap, CONFIG), lecture_scheduled_start=facts.scheduled_start)


def _status(db, lecture_id):
    return db.execute("SELECT match_status, primary_lms_occurrence_id, linked_occurrence_count "
                      "FROM public.bunny_lms_match_status_v WHERE lecture_id = %s", (lecture_id,)).fetchone()


# -- 024 amendments ----------------------------------------------------------------

def _video(db, lecture_id, *, item="ITEM1", retired=False, part=1):
    identity = recording_identity("b!drive", item)
    record = uuid.uuid4()
    db.execute("""INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_part_index,
                      recording_drive_id, recording_item_id, recording_identity, bunny_library_id, video_title,
                      import_status, retired_at, retire_reason)
                  VALUES (%s,%s,%s,'b!drive',%s,%s,%s,%s,'FAILED',%s,%s)""",
               (record, lecture_id, part, item, identity, LIBRARY,
                build_title("Synthetic", "2031-03-04", lecture_id, identity),
                "2031-03-04" if retired else None, "bunny status 6" if retired else None))
    return record


def test_024_failed_import_is_retired_and_retried_as_a_new_row(db):
    lecture_id, _ = _lecture(db)
    first = _video(db, lecture_id)
    # a second LIVE row for the same recording is refused (source, part and token)
    _fails(db, """INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_drive_id,
                      recording_item_id, recording_identity, bunny_library_id, video_title, import_status)
                  SELECT gen_random_uuid(), lecture_id, recording_drive_id, recording_item_id, recording_identity,
                         bunny_library_id, video_title, 'PENDING' FROM public.bunny_videos""")
    # retiring needs a reason
    _fails(db, "UPDATE public.bunny_videos SET retired_at = now() WHERE bunny_video_record_id = %s", (first,))
    db.execute("UPDATE public.bunny_videos SET retired_at = now(), retire_reason = 'bunny status 6' "
               "WHERE bunny_video_record_id = %s", (first,))
    _video(db, lecture_id)                      # same source, part and token: allowed once the first is retired
    assert db.execute("SELECT count(*), count(*) FILTER (WHERE retired_at IS NULL) FROM public.bunny_videos"
                      ).fetchone() == (2, 1)


def test_024_visibility_requires_verified_live_video_and_metadata_refuses_urls(db):
    lecture_id, _ = _lecture(db)
    record = _video(db, lecture_id)
    _fails(db, "UPDATE public.bunny_videos SET visibility_state = 'READY_FOR_LMS'")
    _fails(db, "UPDATE public.bunny_videos SET visibility_state = 'PUBLIC'")
    _fails(db, "UPDATE public.bunny_videos SET metadata = %s",
           (Jsonb({"source": "https://tenant.sharepoint.com/x?tempauth=abc"}),))
    _fails(db, "UPDATE public.bunny_videos SET bunny_status_history = '{}'::jsonb")
    db.execute("UPDATE public.bunny_videos SET bunny_video_id = gen_random_uuid(), import_status = 'IDENTIFIED', "
               "encoding_status = 'VERIFIED', visibility_state = 'READY_FOR_LMS' WHERE bunny_video_record_id = %s",
               (record,))
    # a visible video cannot be retired without being withdrawn first
    _fails(db, "UPDATE public.bunny_videos SET retired_at = now(), retire_reason = 'x'")


# -- 025: decisions, history, links ------------------------------------------------------

def test_verified_decision_is_recorded_once_and_reevaluation_only_touches_it(db):
    lecture_id, facts = _lecture(db)
    occ = occurrence()
    first = _record(db, facts, snapshot(occ))
    second = _record(db, facts, snapshot(occ))
    assert first.changed and first.status == VERIFIED
    assert not second.changed and second.evaluation_id == first.evaluation_id
    assert db.execute("SELECT count(*), max(evaluation_count) FROM public.bunny_lms_match_evaluations "
                      "WHERE lecture_id = %s", (lecture_id,)).fetchone() == (1, 2)
    assert _status(db, lecture_id) == (VERIFIED, occ.occurrence_id, 1)


def test_unevaluated_lecture_is_pending_in_the_view(db):
    lecture_id, _ = _lecture(db)
    assert _status(db, lecture_id) == ("PENDING", None, 0)


def test_new_decision_supersedes_and_keeps_history(db):
    lecture_id, facts = _lecture(db)
    missing = _record(db, facts, snapshot())
    occ = occurrence()
    found = _record(db, facts, snapshot(occ))
    assert (missing.status, found.status, found.previous_status) == (NO_LMS_SESSION, VERIFIED, NO_LMS_SESSION)
    rows = db.execute("SELECT evaluation_id::text, is_current, superseded_by::text FROM public.bunny_lms_match_evaluations "
                      "WHERE lecture_id = %s ORDER BY first_evaluated_at, is_current", (lecture_id,)).fetchall()
    assert rows == [(missing.evaluation_id, False, found.evaluation_id), (found.evaluation_id, True, None)]


def test_verified_match_is_never_repointed_automatically(db):
    lecture_id, facts = _lecture(db)
    original, moved = occurrence(module="MOD-A"), occurrence(module="MOD-B")
    verified = _record(db, facts, snapshot(original))
    stale = _record(db, facts, snapshot(moved))               # the LMS now names a different occurrence
    assert stale.status == STALE
    assert _status(db, lecture_id) == (STALE, None, 0)
    # the original link is history now (is_current cascaded), so the occurrence is free
    assert db.execute("SELECT is_current FROM public.bunny_lms_occurrence_links WHERE evaluation_id = %s",
                      (verified.evaluation_id,)).fetchone() == (False,)
    evidence = db.execute("SELECT evidence FROM public.bunny_lms_match_evaluations WHERE evaluation_id = %s",
                          (stale.evaluation_id,)).fetchone()[0]
    assert evidence["previous_primary_occurrence_id"] == original.occurrence_id
    assert evidence["observed_status"] == VERIFIED
    # the same disagreement again changes nothing
    assert not _record(db, facts, snapshot(moved)).changed
    # the LMS going missing still keeps it STALE (anchored to the verified decision)
    assert _record(db, facts, snapshot()).status == STALE
    # the LMS coming back to the verified answer restores VERIFIED
    restored = _record(db, facts, snapshot(original))
    assert restored.status == VERIFIED and _status(db, lecture_id) == (VERIFIED, original.occurrence_id, 1)


def test_one_occurrence_cannot_belong_to_two_lectures(db):
    (first_id, first), (second_id, second) = _lecture(db), _lecture(db)
    occ = occurrence()
    assert _record(db, first, snapshot(occ)).status == VERIFIED
    clash = _record(db, second, snapshot(occ))
    assert clash.status == CONFLICT
    evidence = db.execute("SELECT evidence FROM public.bunny_lms_match_evaluations WHERE evaluation_id = %s",
                          (clash.evaluation_id,)).fetchone()[0]
    assert evidence["held_by_lectures"] == {occ.occurrence_id: first_id}
    # and the database itself refuses a second current link for the occurrence
    _fails(db, """INSERT INTO public.bunny_lms_occurrence_links (evaluation_id, lecture_id, match_status, is_current,
                      lms_occurrence_id, lms_live_session_id, lms_scheduled_start, link_role)
                  SELECT evaluation_id, lecture_id, 'VERIFIED', true, %s, 'LIVE-X', now(), 'PRIMARY'
                    FROM public.bunny_lms_match_evaluations WHERE evaluation_id = %s""",
           (occ.occurrence_id, clash.evaluation_id))


def test_shared_module_meeting_links_one_lecture_to_several_occurrences(db):
    lecture_id, facts = _lecture(db)
    source, shared = occurrence(module="MOD-SRC"), occurrence(module="MOD-S1")
    outcome = _record(db, facts, snapshot(source, shared, shares={"MOD-S1": "MOD-SRC"}))
    assert outcome.status == VERIFIED and _status(db, lecture_id) == (VERIFIED, source.occurrence_id, 2)
    roles = db.execute("SELECT lms_occurrence_id, link_role, shared_source_module_id FROM public.bunny_lms_occurrence_links "
                       "WHERE evaluation_id = %s ORDER BY link_role", (outcome.evaluation_id,)).fetchall()
    assert roles == [(source.occurrence_id, "PRIMARY", None), (shared.occurrence_id, "SHARED_MODULE", "MOD-SRC")]


def test_out_of_scope_is_an_operator_decision_only_a_verified_match_replaces(db):
    lecture_id, facts = _lecture(db, start=START + timedelta(hours=3))
    repo.declare_out_of_scope(db, lecture_id, lecture_scheduled_start=facts.scheduled_start,
                              actor="operator@example.invalid", reason="Aptem-only programme")
    assert _record(db, facts, snapshot()).status == OUT_OF_LMS_SCOPE
    occ = occurrence(start=facts.scheduled_start, url=None)
    assert _record(db, facts, snapshot(occ)).status == VERIFIED


def test_unparsed_and_suppressed_lectures_are_states_not_absences(db):
    lecture_id, _ = _lecture(db)
    suppressed = lecture(lecture_id=lecture_id, suppressed=True)
    assert _record(db, suppressed, snapshot(occurrence())).status == "LI_DUPLICATE"
    other_id, _ = _lecture(db)
    no_key = lecture(lecture_id=other_id, url="https://example.invalid/none")
    outcome = _record(db, no_key, snapshot())
    assert outcome.status == "NO_MEETING_KEY"
    assert db.execute("SELECT requires_review FROM public.bunny_lms_match_evaluations WHERE evaluation_id = %s",
                      (outcome.evaluation_id,)).fetchone() == (True,)


def test_match_table_constraints(db):
    lecture_id, facts = _lecture(db)
    verified = _record(db, facts, snapshot(occurrence()))
    insert_eval = """INSERT INTO public.bunny_lms_match_evaluations (evaluation_id, lecture_id, rule_version,
                        match_status, requires_review, lecture_scheduled_start, decision_sha256, evidence)
                     VALUES (gen_random_uuid(), %s, 'meeting_key_start_v2', %s, %s, now(), %s, %s)"""
    # a second current decision for the lecture
    _fails(db, insert_eval, (lecture_id, "NO_LMS_SESSION", True, "a" * 64, Jsonb({})))
    other_id, _ = _lecture(db)
    # requires_review must agree with the status
    _fails(db, insert_eval, (other_id, "NO_LMS_SESSION", False, "a" * 64, Jsonb({})))
    _fails(db, insert_eval, (other_id, "MAYBE", True, "a" * 64, Jsonb({})))
    # evidence may not carry URLs or Teams thread ids
    _fails(db, insert_eval, (other_id, "NO_LMS_SESSION", True, "a" * 64, Jsonb({"u": "https://teams.microsoft.com/x"})))
    _fails(db, insert_eval, (other_id, "NO_LMS_SESSION", True, "a" * 64, Jsonb({"t": "19:abc@thread.tacv2"})))
    # a link can only hang off a VERIFIED decision, and a decision has one PRIMARY
    db.execute(insert_eval, (other_id, "NO_LMS_SESSION", True, "b" * 64, Jsonb({})))
    link = """INSERT INTO public.bunny_lms_occurrence_links (evaluation_id, lecture_id, match_status, is_current,
                  lms_occurrence_id, lms_live_session_id, lms_scheduled_start, link_role, shared_source_module_id)
              SELECT evaluation_id, lecture_id, %s, true, %s, 'LIVE-X', now(), %s, %s
                FROM public.bunny_lms_match_evaluations WHERE lecture_id = %s AND is_current"""
    _fails(db, link, ("NO_LMS_SESSION", "OCC-NEW1", "PRIMARY", None, other_id))
    _fails(db, link, ("VERIFIED", "OCC-NEW2", "PRIMARY", None, other_id))   # decision is not VERIFIED
    _fails(db, link, ("VERIFIED", "OCC-NEW3", "PRIMARY", None, lecture_id))  # second PRIMARY
    _fails(db, link, ("VERIFIED", "OCC-NEW4", "SHARED_MODULE", None, lecture_id))  # share needs its source
    # a verified decision with links cannot be edited into another status
    _fails(db, "UPDATE public.bunny_lms_match_evaluations SET match_status = 'AMBIGUOUS' WHERE evaluation_id = %s",
           (verified.evaluation_id,))


# -- 025: LMS-side coverage --------------------------------------------------------

def test_coverage_upsert_tracks_status_changes(db):
    lecture_id, facts = _lecture(db)
    occ = occurrence(series_status="week-meeting", meeting_type="week-additional")
    late = START + timedelta(days=2)
    rows = coverage.classify_snapshot(snapshot(occ), {}, CONFIG, late)
    assert repo.upsert_coverage(db, rows) == {"inserted": 1, "updated": 0}
    assert db.execute("SELECT coverage_status FROM public.bunny_lms_occurrence_coverage").fetchone() == (
        coverage.LMS_SESSION_NOT_DISCOVERED,)
    before = db.execute("SELECT status_changed_at FROM public.bunny_lms_occurrence_coverage").fetchone()[0]
    _record(db, facts, snapshot(occ))
    owners = repo.current_occurrence_owners(db)
    assert owners == {occ.occurrence_id: lecture_id}
    rows = coverage.classify_snapshot(snapshot(occ), owners, CONFIG, late)
    assert repo.upsert_coverage(db, rows) == {"inserted": 0, "updated": 1}
    status, matched, changed = db.execute(
        "SELECT coverage_status, matched_lecture_id::text, status_changed_at FROM public.bunny_lms_occurrence_coverage").fetchone()
    assert (status, matched) == (coverage.MATCHED, lecture_id) and changed >= before
    _fails(db, "UPDATE public.bunny_lms_occurrence_coverage SET matched_lecture_id = NULL")
    _fails(db, "UPDATE public.bunny_lms_occurrence_coverage SET coverage_status = 'TEST_EXCLUDED', matched_lecture_id = NULL")


# -- 026: queue, events, heartbeats -------------------------------------------------

def test_enqueue_is_idempotent_per_live_subject(db):
    lecture_id, _ = _lecture(db)
    job, created = queue.enqueue(db, job_kind="MATCH_LMS", subject_key=f"lecture:{lecture_id}", lecture_id=lecture_id)
    again, created_again = queue.enqueue(db, job_kind="MATCH_LMS", subject_key=f"lecture:{lecture_id}")
    assert created and not created_again and again["job_id"] == job["job_id"]
    claimed = queue.claim(db, "worker-a", ["MATCH_LMS"])
    assert claimed["job_id"] == job["job_id"] and claimed["attempt_count"] == 1
    assert queue.succeed(db, job["job_id"], "worker-a")
    fresh, created = queue.enqueue(db, job_kind="MATCH_LMS", subject_key=f"lecture:{lecture_id}")
    assert created and fresh["job_id"] != job["job_id"]          # a finished job does not block new work


def test_claim_order_not_before_and_kinds(db):
    low, _ = queue.enqueue(db, job_kind="MATCH_LMS", subject_key="s:low", priority=10)
    high, _ = queue.enqueue(db, job_kind="MATCH_LMS", subject_key="s:high", priority=100)
    queue.enqueue(db, job_kind="MATCH_LMS", subject_key="s:later", priority=999,
                  not_before=START + timedelta(days=3650))
    queue.enqueue(db, job_kind="IMPORT_VIDEO", subject_key="s:other-kind", priority=1000)
    assert queue.claim(db, "w", ["MATCH_LMS"])["job_id"] == high["job_id"]
    assert queue.claim(db, "w", ["MATCH_LMS"])["job_id"] == low["job_id"]
    assert queue.claim(db, "w", ["MATCH_LMS"]) is None


def test_retry_backoff_then_failed_and_lease_takeover(db):
    job, _ = queue.enqueue(db, job_kind="RESOLVE_RECORDING", subject_key="s:r", max_attempts=2)
    queue.claim(db, "w1", ["RESOLVE_RECORDING"])
    assert queue.retry(db, job["job_id"], "w1", error_code="DNS", error="gaierror") == "RETRY_WAIT"
    assert queue.claim(db, "w1", ["RESOLVE_RECORDING"]) is None             # not due yet
    db.execute("UPDATE public.bunny_publisher_jobs SET next_attempt_at = now() - interval '1 second' WHERE job_id = %s",
               (job["job_id"],))
    assert queue.claim(db, "w1", ["RESOLVE_RECORDING"])["attempt_count"] == 2
    # the lease expires; another worker takes over and the first can no longer finish it
    db.execute("UPDATE public.bunny_publisher_jobs SET locked_at = now() - interval '1 hour', max_attempts = 3 "
               "WHERE job_id = %s", (job["job_id"],))
    assert queue.claim(db, "w2", ["RESOLVE_RECORDING"])["locked_by"] == "w2"
    assert not queue.succeed(db, job["job_id"], "w1")
    assert queue.retry(db, job["job_id"], "w2", error_code="DNS", error="gaierror") == "FAILED"
    assert queue.job(db, job["job_id"])["finished_at"] is not None


def test_queue_constraints_events_and_heartbeats(db):
    _fails(db, "INSERT INTO public.bunny_publisher_jobs (job_id, job_kind, subject_key) VALUES (gen_random_uuid(), 'DELETE_ALL', 'x')")
    _fails(db, "INSERT INTO public.bunny_publisher_jobs (job_id, job_kind, subject_key, payload) VALUES "
               "(gen_random_uuid(), 'IMPORT_VIDEO', 'x', %s)", (Jsonb({"url": "https://x.invalid/file?sig=abc"}),))
    _fails(db, "INSERT INTO public.bunny_publisher_jobs (job_id, job_kind, subject_key, status) VALUES "
               "(gen_random_uuid(), 'IMPORT_VIDEO', 'x', 'RUNNING')")                 # running needs a lease
    job, _ = queue.enqueue(db, job_kind="IMPORT_VIDEO", subject_key="s:e", payload={"drive_id": "b!d", "item_id": "I"})
    assert queue.event(db, "IMPORT_SUBMITTING", job_id=job["job_id"], detail={"token": "rec-00000000"}) > 0
    _fails(db, "INSERT INTO public.bunny_publisher_events (event) VALUES ('lower case')")
    _fails(db, "INSERT INTO public.bunny_publisher_events (event, detail) VALUES ('X_URL', %s)",
           (Jsonb({"u": "http://x.invalid"}),))
    queue.heartbeat(db, "host:1", version="p0a", current_job_id=job["job_id"])
    queue.heartbeat(db, "host:1", version="p0a")
    assert db.execute("SELECT count(*), max(version) FROM public.bunny_publisher_heartbeats").fetchone() == (1, "p0a")


# -- rollbacks and static shape -------------------------------------------------------

def test_rollbacks_refuse_with_rows_then_drop_and_reapply_cleanly(db):
    lecture_id, facts = _lecture(db)
    _record(db, facts, snapshot(occurrence()))
    queue.enqueue(db, job_kind="MATCH_LMS", subject_key="s:rb")
    _video(db, lecture_id)
    for path in ROLLBACKS:
        with pytest.raises(psycopg.errors.RaiseException):
            with db.transaction():
                db.execute(body(path))
    for table in ("bunny_publisher_events", "bunny_publisher_jobs", "bunny_publisher_heartbeats", "bunny_lms_occurrence_links",
                  "bunny_lms_match_evaluations", "bunny_lms_occurrence_coverage", "bunny_caption_current",
                  "bunny_caption_publications", "bunny_videos"):
        db.execute(f"DELETE FROM public.{table}")
    for path in ROLLBACKS:
        db.execute(body(path))
    assert applied(db) == []
    for path in MIGRATIONS * 2:                    # idempotent re-apply
        db.execute(body(path))
    assert applied(db) == list(NEW_TABLES)


@pytest.mark.parametrize("path", MIGRATIONS[1:])
def test_new_migrations_touch_no_existing_table(path):
    text = path.read_text(encoding="utf-8")
    code = re.sub(r"'(?:[^']|'')*'", "''", re.sub(r"--[^\n]*", "", text))      # comments, string literals
    code = re.sub(r"\bON (DELETE|UPDATE)\b", "", code)
    assert not re.search(r"\b(ALTER|DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b", code)
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.([a-z_]+)", text))
    assert created <= set(NEW_TABLES) and created
