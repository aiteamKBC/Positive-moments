"""
RELEASE GATE: late attendance through the REAL shared orchestrator, two cycles.

Nothing below the cycle is called directly. Every cycle is
`PipelineOrchestrator.run_window` with the real `PipelineStateResolver`, the
real `StageRunner` and every real service and repository under it - transcript
selection, canonical cues, speakers, attendance resolution from the external
`kbc_attendance` source, engagement, QA, rendering, the two-pass automated
legacy sync with its sync-safety gate, the writer and the Perfect planner.
Only three things are stubs, because each would otherwise leave the machine:
the model provider (counted), the n8n read-only precheck, and Graph (off).

Cycle 1  attendance source empty
         -> QA from the transcript -> render -> qa_doctors_sessions
         -> attendance fields UNKNOWN (NULL), Item 7 the model's, Perfect pending
Cycle 2  attendance lands in kbc_attendance
         -> RECOVER_ATTENDANCE, which inside the one action re-resolves the
            attendance snapshot, calculates lecture-scoped engagement and runs
            the DETERMINISTIC QA refresh on the stored model answer, then
            re-renders -> SYNC_LEGACY_QA
         -> zero provider calls, no RUN_QA, the SAME coded-owned row updated in
            place, no second row, foreign-owned columns byte-identical, Item 7
            moved only by the deterministic engagement rule, the other ten
            items untouched, Perfect considered only now

And the answer that cannot be refreshed: an evaluation bought under the old
json_object_v1 contract that still states the pre-fix attendance zeros goes to
review - UNVERIFIED_ATTENDANCE_ANSWER_NOT_REFRESHABLE - and stays there, cycle
after cycle, without a refresh loop and without a paid regeneration.

The daemon commits once per cycle (app/orchestration/daemon.py). These tests
roll everything back, so between cycles `_commit_point` restores the one
property a real commit gives the next cycle: its rows are strictly older.

Every connection is rolled back.
"""
from __future__ import annotations

import dataclasses

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.pipeline_observations import LegacyObservationRepository
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.orchestration import runner as runner_module
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.reconciliation import bucket_for
from app.orchestration.runner import StageRunner
from app.orchestration.stages import (
    CALCULATE_ENGAGEMENT,
    MANUAL_REVIEW_REQUIRED,
    NOTHING_TO_DO,
    RECOVER_ATTENDANCE,
    REFRESH_DETERMINISTIC_QA,
    RUN_QA,
    SYNC_LEGACY_QA,
    WAIT_FOR_ATTENDANCE_SOURCE,
)
from app.orchestration.state import PipelineStateResolver
from app.qa.inputs import ATTENDANCE_PENDING
from tests.integration.seeding import (
    DAY,
    TRAINER,
    graph_id,
    insert,
    seed_attendance_source,
    seed_lecture,
    sha,
    webvtt,
)
from tests.integration.test_attendance_optional_qa_persistence import Provider
from tools.integration_db_guard import assert_isolated


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    _COMMIT_POINTS.pop(id(connection), None)
    try:
        assert_isolated(connection)
        yield connection
    finally:
        connection.rollback()
        connection.close()


class CountingProvider(Provider):
    """The only model in the building. Every call is counted."""

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
    """n8n's legacy QA node reported disabled - read-only, never an HTTP call."""

    def check(self):
        return {"status": "LEGACY_QA_DISABLED", "legacy_qa_node_disabled": True,
                "reason": "LEGACY_QA_DISABLED", "writes_permitted": True,
                "read_only": True, "http_method": "GET", "n8n_modified": False}


def _orchestrator():
    settings = dataclasses.replace(Settings.from_environment(),
                                   qa_model_api_key="not-a-real-key",
                                   qa_model_base_url="https://provider.invalid/v1")
    resolver = PipelineStateResolver(legacy_observations=LegacyObservationRepository())
    return resolver, PipelineOrchestrator(
        resolver=resolver, runner=StageRunner(settings=settings, allow_graph=False),
        preflight=StubPreflight())


SUBJECT = "Andrew-Scheduling Professional (SP) Jan 2026"
ATTENDEES = ("Synthetic Learner 1", "Synthetic Learner 2", "Synthetic Learner 3")

# Owned by the recording-link and Positive Clips workflows, never by the QA
# writer. Filled in between the cycles, as those workflows do in production.
FOREIGN = {"recording_url": "https://recording.invalid/keep",
           "recording_link_status": "LINKED", "recording_item_id": "item-keep",
           "recording_drive_id": "drive-keep", "recording_id": "rec-keep",
           "recap_url": "https://recap.invalid/keep",
           "transcript_url": "https://transcript.invalid/keep",
           "positive_clips": '{"clips": [1, 2]}', "clips_status": "done",
           "clips_count": 2, "clips_analysis_completeness": "COMPLETE"}


def _cues(learner_speakers):
    """Two hours, trainer throughout; the named learners speak once each."""
    cues = [(minute * 60_000, minute * 60_000 + 30_000, TRAINER, f"Synthetic line {minute}.")
            for minute in range(0, 120, 10)]
    for index, name in enumerate(learner_speakers):
        start = (5 + index * 20) * 60_000
        cues.append((start, start + 20_000, name, f"Synthetic question {index}."))
    cues.append((7_170_000, 7_200_000, TRAINER, "Synthetic close."))
    return sorted(cues)


def _discovered_lecture(db, n, *, learner_speakers=ATTENDEES[:2]):
    """A lecture exactly as discovery leaves it: artifact fetched, nothing chosen."""
    lecture = seed_lecture(db, transcript_id=graph_id(880 + n), meeting_id=f"MTG-LATE-{n}",
                           subject=SUBJECT, select=False)
    content = webvtt(_cues(learner_speakers))
    TranscriptArtifactRepository().store_content(
        db, lecture["artifact_ids"][0], raw_text=content, content_sha256=sha(content),
        content_bytes=len(content.encode()), content_format="text/vtt",
        speaker_attribution=True)
    for student in range(2):
        insert(db, "kbc_users_data", ID=str(880000 + n * 10 + student),
               FullName=f"Synthetic Student {n}-{student}", Group=SUBJECT,
               **{"Program-Status": "Active"})
    return lecture


_COMMIT_POINTS: dict = {}


def _commit_point(db):
    """
    Emulate the daemon's commit between two cycles.

    Production commits once per cycle, so every row a cycle writes carries
    that cycle's own now(). A rolled-back test runs every cycle in ONE
    transaction, where now() never moves and "the latest snapshot / render /
    evaluation" would tie with the previous cycle's. Moving every timestamp
    this transaction has written so far back by one hour - every earlier cycle
    moving with it, so their order is kept too - restores exactly the ordering
    committed cycles have. Nothing else about the rows changes.
    """
    earlier = _COMMIT_POINTS.get(id(db), 0)
    _COMMIT_POINTS[id(db)] = earlier + 1
    columns = db.execute("""
        SELECT c.table_name, c.column_name FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
           AND c.data_type = 'timestamp with time zone'""").fetchall()
    for table, column in columns:
        db.execute(f'UPDATE public."{table}" SET "{column}" = "{column}" - interval '
                   f"'1 hour' WHERE \"{column}\" BETWEEN now() - make_interval(hours => %s)"
                   " AND now()", (earlier,))


def _cycle(orchestrator, db, lecture):
    before = CountingProvider.calls_total
    summary = orchestrator.run_window(db, DAY, lecture_ids=[str(lecture["lecture_id"])])
    (item,) = summary["lectures"]
    return item, CountingProvider.calls_total - before


def _one(db, sql, *params):
    return db.execute(sql, params).fetchone()


def _sessions(db, lecture):
    return db.execute(
        'SELECT session_id, "Engagement", engagement_score, met_count, partial_count,'
        '       not_met_count FROM public.qa_doctors_sessions WHERE meeting_id = %s',
        (lecture["meeting_id"],)).fetchall()


def _legacy_total(db):
    return _one(db, "SELECT count(*) FROM public.qa_doctors_sessions")[0]


def _foreign_bytes(db, session_id):
    """The foreign-owned columns, as the exact bytes PostgreSQL holds."""
    return _one(db, "SELECT " + ", ".join(
        f"convert_to(coalesce({column}::text, '<NULL>'), 'UTF8')" for column in FOREIGN)
        + " FROM public.qa_doctors_sessions WHERE session_id = %s", session_id)


def _checklist(db, session_id):
    return {row[0]: row[1:] for row in db.execute(
        "SELECT checklist_order, status, evidence FROM public.qa_doctors_checklist_items"
        " WHERE session_id = %s", (session_id,)).fetchall()}


def _evaluations(db, lecture):
    return db.execute("""
        SELECT evaluation_id, qa_status, attended_count, engagement_percentage,
               engagement_score, item7_override_applied, final_item7_status,
               metadata ->> 'attendance_flag', metadata ? 'deterministic_refresh',
               ai_raw_output::text, metadata ->> 'model_input_fingerprint'
          FROM public.lecture_qa_evaluations WHERE lecture_id = %s
         ORDER BY updated_at, created_at""", (lecture["lecture_id"],)).fetchall()


def _perfect_rows(db, lecture):
    return _one(db, "SELECT count(*) FROM public.qa_perfect_lectures p"
                    " JOIN public.lecture_perfect_lecture_legacy_writes w"
                    "   ON w.legacy_lecture_key = p.lecture_key"
                    " WHERE w.lecture_id = %s", lecture["lecture_id"])[0]


def _ownership(db, lecture):
    return db.execute("SELECT write_status, legacy_session_id"
                      "  FROM public.lecture_qa_legacy_writes WHERE lecture_id = %s"
                      " ORDER BY written_at", (lecture["lecture_id"],)).fetchall()


def _stage(resolver, db, lecture, name):
    return resolver.for_lecture(db, str(lecture["lecture_id"]))["stages"][name]


# ===========================================================================
# the two-cycle journey
# ===========================================================================

@pytest.mark.parametrize("speakers, expected_item7", [
    (ATTENDEES[:2], "Partially Met"),   # 2 of 3 attendees spoke
    (ATTENDEES, "Met"),                 # all 3 spoke: Item 7 stays Met
], ids=["item7-moves", "item7-stays"])
def test_late_attendance_refreshes_the_same_row_for_zero_provider_calls(
        db, speakers, expected_item7):
    resolver, orchestrator = _orchestrator()
    lecture = _discovered_lecture(db, 1 if expected_item7 == "Met" else 2,
                                  learner_speakers=speakers)
    legacy_before_everything = _legacy_total(db)

    # --- CYCLE 1: no attendance. QA, render, qa_doctors_sessions. -------------
    item, calls = _cycle(orchestrator, db, lecture)
    assert calls == 1 and item["provider_calls"] == 1            # CYCLE_1_PROVIDER_CALLS
    for action in (RUN_QA, "RENDER_QA", SYNC_LEGACY_QA, "EVALUATE_PERFECT"):
        assert action in item["actions"], item["actions"]
    assert item["final_action"] == WAIT_FOR_ATTENDANCE_SOURCE
    assert item["error_code"] is None

    ((session_id, engagement, engagement_score, met, partial, not_met),) = \
        _sessions(db, lecture)
    assert (engagement, engagement_score) == (None, None)       # UNKNOWN, not zero
    assert (met, partial, not_met) == (11, 0, 0)                 # Item 7: the model's
    ((cycle1_id, status, attended, percentage, score, override, _item7, flag, refreshed,
      answer, model_fingerprint),) = _evaluations(db, lecture)
    assert status == "COMPLETED"
    assert (attended, percentage, score, override) == (None, None, None, False)
    assert flag == ATTENDANCE_PENDING and refreshed is False
    assert _ownership(db, lecture) == [("WRITTEN", session_id)]

    perfect = _stage(resolver, db, lecture, "PERFECT_ELIGIBILITY")
    assert perfect["state"] == "WAITING"
    assert perfect["reason"] == "PENDING_ATTENDANCE_DATA"
    assert _perfect_rows(db, lecture) == 0
    state = resolver.for_lecture(db, str(lecture["lecture_id"]))
    assert state["attendance_flag"] == ATTENDANCE_PENDING
    assert bucket_for(state) == "complete"                       # flagged, not blocked

    # --- between cycles: other workflows fill their own columns -------------
    db.execute("UPDATE public.qa_doctors_sessions SET " + ", ".join(
        f"{column} = %s" + ("::jsonb" if column == "positive_clips" else "")
        for column in FOREIGN) + " WHERE session_id = %s", (*FOREIGN.values(), session_id))
    _commit_point(db)
    foreign_before = _foreign_bytes(db, session_id)
    checklist_before = _checklist(db, session_id)
    legacy_before = _legacy_total(db)                            # LEGACY_ROW_COUNT_BEFORE
    assert legacy_before == legacy_before_everything + 1

    # --- attendance lands in the external source ------------------------------
    seed_attendance_source(db, lecture, present=3)

    # --- CYCLE 2 -----------------------------------------------------------------
    item, calls = _cycle(orchestrator, db, lecture)
    assert calls == 0 and item["provider_calls"] == 0            # CYCLE_2_PROVIDER_CALLS
    assert RUN_QA not in item["actions"]                          # RUN_QA_IN_CYCLE_2
    assert item["actions"][0] == RECOVER_ATTENDANCE
    assert SYNC_LEGACY_QA in item["actions"]
    assert item["error_code"] is None and item["final_action"] == NOTHING_TO_DO

    # RECOVER_ATTENDANCE did the three steps: a new authoritative snapshot ...
    snapshots = db.execute("""
        SELECT source_row_count, present_row_count FROM public.lecture_attendance_snapshots
         WHERE lecture_id = %s ORDER BY created_at""", (lecture["lecture_id"],)).fetchall()
    assert snapshots[0] == (0, 0) and snapshots[-1] == (3, 3)
    # ... lecture-scoped engagement on it (CALCULATE_ENGAGEMENT) ...
    engagement_row = _one(db, """
        SELECT m.attended_count, m.spoke_count FROM public.lecture_engagement_metrics m
          JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = m.attendance_snapshot_id
         WHERE m.lecture_id = %s AND s.source_row_count = 3""", lecture["lecture_id"])
    assert engagement_row == (3, len(speakers))
    # ... and the DETERMINISTIC refresh (REFRESH_DETERMINISTIC_QA): a new
    # evaluation reusing the byte-identical stored model answer.
    evaluations = _evaluations(db, lecture)
    assert len(evaluations) == 2 and evaluations[0][0] == cycle1_id
    (refreshed_id, status, attended, _pct, score, override, item7, flag, refreshed,
     refreshed_answer, refreshed_fingerprint) = evaluations[1]
    assert refreshed is True                                     # REFRESH_DETERMINISTIC_QA_IN_CYCLE_2
    assert refreshed_answer == answer                             # same model answer
    assert refreshed_fingerprint == model_fingerprint             # same lineage
    assert (status, attended, flag) == ("COMPLETED", 3, None)
    assert item7 == expected_item7

    # --- the published row: the SAME one, updated in place --------------------
    rows = _sessions(db, lecture)
    assert len(rows) == 1 and rows[0][0] == session_id           # SAME_SESSION_ID
    assert _legacy_total(db) == legacy_before                    # LEGACY_ROW_COUNT_AFTER
    assert rows[0][1] is not None and rows[0][2] is not None      # real engagement now
    checklist_after = _checklist(db, session_id)
    # Item 7: the deterministic engagement rule's verdict, with its evidence
    # now stating the real counts - whether or not the status moved.
    assert checklist_after[7][0] == expected_item7
    assert "Engagement score" in checklist_after[7][1]
    for order in range(1, 12):                                    # the other 10: byte-equal
        if order != 7:
            assert checklist_after[order] == checklist_before[order], order
    assert _foreign_bytes(db, session_id) == foreign_before       # FOREIGN_FIELDS_PRESERVED
    assert [status for status, _ in _ownership(db, lecture)][-1] == "UPDATED"
    assert {sid for _, sid in _ownership(db, lecture)} == {session_id}

    # --- Perfect: considered now, and only now --------------------------------
    perfect = _stage(resolver, db, lecture, "PERFECT_ELIGIBILITY")
    assert perfect["state"] == "COMPLETE"
    assert perfect["reason"] != "PENDING_ATTENDANCE_DATA"
    if expected_item7 != "Met":
        assert perfect["reason"].startswith("NOT_ELIGIBLE")
        assert _perfect_rows(db, lecture) == 0
    state = resolver.for_lecture(db, str(lecture["lecture_id"]))
    assert state["attendance_flag"] is None
    assert state["attendance_source_authoritative"] is True

    # --- CYCLE 3: nothing left to buy or write ----------------------------------
    _commit_point(db)
    before = (_sessions(db, lecture), _foreign_bytes(db, session_id),
              len(_evaluations(db, lecture)), _legacy_total(db))
    item, calls = _cycle(orchestrator, db, lecture)
    assert calls == 0 and RUN_QA not in item["actions"]
    assert REFRESH_DETERMINISTIC_QA not in item["actions"]
    assert (_sessions(db, lecture), _foreign_bytes(db, session_id),
            len(_evaluations(db, lecture)), _legacy_total(db)) == before


# ===========================================================================
# the answer that cannot be refreshed
# ===========================================================================

def _published_old_contract_false_zero(db, orchestrator, n):
    """
    The 2026-09-17 Martech shape: published before attendance became optional,
    under the json_object_v1 contract, stating the empty snapshot's zeros as
    fact on a coded-owned row. Built by the real cycle 1, then aged into the
    pre-fix shape it had in production.
    """
    lecture = _discovered_lecture(db, n)
    item, calls = _cycle(orchestrator, db, lecture)
    assert calls == 1 and item["error_code"] is None
    ((session_id, *_),) = _sessions(db, lecture)
    db.execute("""
        UPDATE public.lecture_qa_evaluations
           SET attended_count = 0, engagement_percentage = 0.00, engagement_score = 1,
               metadata = (metadata - 'attendance_flag')
                          || '{"provider_contract_version": "json_object_v1"}'::jsonb
         WHERE lecture_id = %s""", (lecture["lecture_id"],))
    db.execute('UPDATE public.qa_doctors_sessions SET "Engagement" = 0, engagement_score = 1'
               " WHERE session_id = %s", (session_id,))
    _commit_point(db)
    return lecture, session_id


def _frozen(db, lecture, session_id):
    return (_sessions(db, lecture), _checklist(db, session_id), _evaluations(db, lecture),
            _ownership(db, lecture),
            _one(db, "SELECT count(*) FROM public.lecture_qa_generation_attempts"
                     " WHERE lecture_id = %s", lecture["lecture_id"])[0])


def test_an_old_contract_answer_on_missing_attendance_goes_to_review_and_stays(db):
    resolver, orchestrator = _orchestrator()
    lecture, session_id = _published_old_contract_false_zero(db, orchestrator, 3)
    frozen = _frozen(db, lecture, session_id)

    stage = _stage(resolver, db, lecture, "QA_EVALUATION")
    assert stage["state"] == "REVIEW_REQUIRED"
    assert stage["action"] == MANUAL_REVIEW_REQUIRED
    assert stage["reason"] == "UNVERIFIED_ATTENDANCE_ANSWER_NOT_REFRESHABLE"
    assert stage["provider_contract_version"] == "json_object_v1"
    state = resolver.for_lecture(db, str(lecture["lecture_id"]))
    assert bucket_for(state) == "review"                          # NEEDS_REVIEW
    assert state["attendance_flag"] == ATTENDANCE_PENDING

    # Three cycles: never a refresh, never a regeneration, never a write.
    for _cycle_number in range(3):
        item, calls = _cycle(orchestrator, db, lecture)
        assert calls == 0 and item["provider_calls"] == 0
        for action in (RUN_QA, REFRESH_DETERMINISTIC_QA, CALCULATE_ENGAGEMENT,
                       SYNC_LEGACY_QA):
            assert action not in item["actions"], item["actions"]
        assert item["final_action"] == MANUAL_REVIEW_REQUIRED
        assert _frozen(db, lecture, session_id) == frozen
        _commit_point(db)
        frozen = _frozen(db, lecture, session_id)


def test_an_old_contract_answer_stays_in_review_when_attendance_arrives(db):
    """Attendance landing does not make an unreusable answer reusable: recovery
    fails closed rather than buying a new generation or rewriting the row."""
    resolver, orchestrator = _orchestrator()
    lecture, session_id = _published_old_contract_false_zero(db, orchestrator, 4)
    published = (_sessions(db, lecture), _checklist(db, session_id))
    seed_attendance_source(db, lecture, present=3)

    for _cycle_number in range(2):
        item, calls = _cycle(orchestrator, db, lecture)
        assert calls == 0 and item["provider_calls"] == 0
        assert RUN_QA not in item["actions"]
        assert SYNC_LEGACY_QA not in item["actions"]
        assert (_sessions(db, lecture), _checklist(db, session_id)) == published
        _commit_point(db)
    state = resolver.for_lecture(db, str(lecture["lecture_id"]))
    assert bucket_for(state) == "review"
    assert state["next_executable_action"] == MANUAL_REVIEW_REQUIRED
    assert not [row for row in _evaluations(db, lecture) if row[8]]   # no refresh row


# ===========================================================================
# regression: "current" is lineage, never "touched last"
# ===========================================================================

def test_a_superseded_render_or_engagement_touched_later_is_never_taken_as_current(db):
    """
    Found by the two-cycle test above: the cycle that recovers attendance
    re-upserts the SUPERSEDED evaluation's render and engagement rows too, and
    every upsert stamps updated_at = now(). Inside that cycle's transaction
    the old and new rows then tie, and a newest-by-timestamp pick fell to a
    uuid - choosing the old render about half the time, which reported the
    render stale, offered RENDER_QA for ever and never published the real
    engagement. Worst case here: the superseded rows touched strictly LATER.
    """
    from app.qa.recovery import recovery_state

    resolver, orchestrator = _orchestrator()
    lecture = _discovered_lecture(db, 5)
    _cycle(orchestrator, db, lecture)
    _commit_point(db)
    seed_attendance_source(db, lecture, present=3)
    item, _calls = _cycle(orchestrator, db, lecture)
    assert item["final_action"] == NOTHING_TO_DO
    _commit_point(db)

    (current_evaluation,) = [row[0] for row in _evaluations(db, lecture) if row[8]]
    db.execute("""UPDATE public.lecture_qa_rendered_sessions
                     SET updated_at = now() + interval '1 minute'
                   WHERE lecture_id = %s AND evaluation_id <> %s""",
               (lecture["lecture_id"], current_evaluation))
    db.execute("""UPDATE public.lecture_engagement_metrics m
                     SET updated_at = now() + interval '1 minute'
                    FROM public.lecture_attendance_snapshots s
                   WHERE s.snapshot_id = m.attendance_snapshot_id
                     AND m.lecture_id = %s AND s.source_row_count = 0""",
               (lecture["lecture_id"],))

    downstream = recovery_state(db, str(lecture["lecture_id"]))
    assert downstream["rendered"]["evaluation_id"] == str(current_evaluation)
    assert downstream["engagement"]["is_current_snapshot"] is True
    assert downstream["next_action"] not in (REFRESH_DETERMINISTIC_QA,
                                             "RECALCULATE_ENGAGEMENT", "REFRESH_RENDER")
    state = resolver.for_lecture(db, str(lecture["lecture_id"]))
    assert state["stages"]["QA_RENDER"]["state"] == "COMPLETE"
    assert state["next_executable_action"] == NOTHING_TO_DO

    before = (_sessions(db, lecture), len(_evaluations(db, lecture)))
    item, calls = _cycle(orchestrator, db, lecture)
    assert calls == 0 and item["actions"] == []
    assert (_sessions(db, lecture), len(_evaluations(db, lecture))) == before
