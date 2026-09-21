"""
Phase 3C1 unit tests: write modes, the ownership policy, the legacy mapping,
digests, and the bounded model-generation cap.

Pure functions and stubs only - no database, no model, no legacy table.
"""
import uuid
from decimal import Decimal

import pytest

from app.qa.checklist import CHECKLIST_ITEMS
from app.writer.mapping import (
    CHECKLIST_COLUMNS,
    SESSION_COLUMNS,
    WRITER_VERSION,
    PayloadInvariantError,
    checklist_rows,
    diff_checklist,
    diff_session,
    digest,
    legacy_boolean,
    payload_is_valid,
    session_row,
    validate_checklist,
)
from app.writer.modes import (
    BLOCKED_BACKFILL_NOT_AUTHORISED,
    BLOCKED_INVALID_PAYLOAD,
    BLOCKED_NOT_READY,
    CANARY_NEW_ONLY,
    DEFAULT_MODE,
    DISABLED,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PRODUCTION_NEW_ONLY,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
    WriterModeError,
    plan_decision,
    validate_mode,
    writes_enabled,
)


def rendered(**overrides):
    base = {
        "rendered_session_id": uuid.UUID(int=1), "lecture_id": uuid.UUID(int=2),
        "evaluation_id": uuid.UUID(int=3), "render_status": "RENDERED",
        "qa_status": "COMPLETED", "source_fingerprint": "a" * 64,
        "session_id": "TRANSCRIPT-1", "meeting_id": "MEETING-1", "subject": "Test Lecture",
        "trainer": "Morgan Trainerfield", "legacy_date": "2026-09-04",
        "canonical_session_date": "2026-09-04", "duration": "2 hours 24 minutes",
        "duration_score": 5, "engagement": Decimal("35.71"), "engagement_score": 2,
        "met_count": 10, "partial_count": 0, "not_met_count": 1,
        "teaching_quality_rating": 4, "teaching_quality_comments": "Clear.",
        "overall_judgement": "Solid.", "cancelled_session": False,
        "lms_module": "Test Lecture", "lms_students_count": 2,
        "lms_students": {"students": [{"ID": 1, "FullName": "Avery Holt"}]},
        "strengths": {"strength_1": {"title": "a"}},
        "areas_for_development": {"area_1": {"title": "b"}},
        "ksb_coverage": {"ksb_1": {"type": "Skill", "title": "c"}},
    }
    base.update(overrides)
    return base


def items(session_id="TRANSCRIPT-1", count=11, statuses=None):
    return [{"checklist_order": order, "checklist_item": CHECKLIST_ITEMS[order - 1],
             "status": (statuses or {}).get(order, "Met"), "session_id": session_id,
             "session_id_match": f"{session_id}_{order}", "evidence": f"evidence {order}"}
            for order in range(1, count + 1)]


# --- 1. defaults ----------------------------------------------------------------

def test_the_default_mode_never_writes():
    assert DEFAULT_MODE == DRY_RUN
    assert writes_enabled(DRY_RUN) is False
    assert writes_enabled(DISABLED) is False
    assert writes_enabled(CANARY_NEW_ONLY) is True
    assert writes_enabled(PRODUCTION_NEW_ONLY) is True
    assert writes_enabled(EXPLICIT_BACKFILL) is True


def test_an_unknown_mode_is_refused():
    with pytest.raises(WriterModeError):
        validate_mode("JUST_WRITE_IT")
    with pytest.raises(WriterModeError):
        writes_enabled("")


def test_the_cli_default_is_dry_run():
    from app.cli.main import build_parser
    args = build_parser().parse_args(["write-legacy-qa", "--date", "2026-09-04"])
    assert args.mode == DRY_RUN
    assert args.allow_update_existing is False


# --- 2-3. the legacy mapping -------------------------------------------------------

def test_session_mapping_covers_exactly_the_twenty_two_legacy_columns():
    row = session_row(rendered())
    assert list(row) == list(SESSION_COLUMNS)
    assert len(SESSION_COLUMNS) == 22
    # Columns owned by other workflows are absent, so they can never be cleared.
    for foreign in ("positive_clips", "clips_status", "recording_url", "transcript_id",
                    "cancellation_reason", "clips_review_queue"):
        assert foreign not in row


def test_session_mapping_values_come_from_the_rendered_payload():
    row = session_row(rendered())
    assert row["session_id"] == "TRANSCRIPT-1"
    assert row["Engagement"] == Decimal("35.71")
    assert row["date"] == "2026-09-04"
    assert row["duration"] == "2 hours 24 minutes"
    assert row["trainer"] == "Morgan Trainerfield"


def test_cancelled_session_is_written_as_legacy_text():
    # The live column is TEXT and n8n wrote JavaScript booleans into it.
    assert legacy_boolean(True) == "true"
    assert legacy_boolean(False) == "false"
    assert legacy_boolean(None) is None
    assert session_row(rendered(cancelled_session=True))["cancelled_session"] == "true"


def test_a_missing_trainer_is_refused_because_the_column_is_not_null():
    with pytest.raises(PayloadInvariantError):
        session_row(rendered(trainer=None))
    with pytest.raises(PayloadInvariantError):
        session_row(rendered(session_id=None))


def test_checklist_mapping_covers_exactly_the_six_legacy_columns():
    rows = checklist_rows(items(), "TRANSCRIPT-1")
    assert list(rows[0]) == list(CHECKLIST_COLUMNS)
    assert rows[0]["evidence"] == "evidence 1"


# --- 4-7. invariants and compatibility keys ------------------------------------------

def test_exactly_eleven_rows_are_required():
    with pytest.raises(PayloadInvariantError):
        checklist_rows(items(count=10), "TRANSCRIPT-1")
    # A twelfth row cannot exist canonically, so it is built by hand.
    extra = items() + [{"checklist_order": 12, "checklist_item": "12) Invented",
                        "status": "Met", "session_id": "TRANSCRIPT-1",
                        "session_id_match": "TRANSCRIPT-1_12", "evidence": "x"}]
    with pytest.raises(PayloadInvariantError):
        checklist_rows(extra, "TRANSCRIPT-1")


def test_orders_must_be_one_through_eleven():
    broken = items()
    broken[3]["checklist_order"] = 99
    with pytest.raises(PayloadInvariantError):
        validate_checklist(sorted(broken, key=lambda row: row["checklist_order"]),
                           "TRANSCRIPT-1")


def test_session_id_match_must_match_the_session_and_be_unique():
    broken = items()
    broken[2]["session_id_match"] = "OTHER_3"
    with pytest.raises(PayloadInvariantError):
        checklist_rows(broken, "TRANSCRIPT-1")
    duplicated = items()
    duplicated[5]["session_id_match"] = duplicated[4]["session_id_match"]
    with pytest.raises(PayloadInvariantError):
        checklist_rows(duplicated, "TRANSCRIPT-1")


def test_session_id_match_format_is_session_id_underscore_order():
    rows = checklist_rows(items(), "TRANSCRIPT-1")
    assert [row["session_id_match"] for row in rows] == [
        f"TRANSCRIPT-1_{order}" for order in range(1, 12)]


def test_a_null_status_is_refused():
    broken = items()
    broken[0]["status"] = None
    with pytest.raises(PayloadInvariantError):
        checklist_rows(broken, "TRANSCRIPT-1")


def test_payload_is_valid_reports_instead_of_raising():
    ok, error = payload_is_valid(rendered(), items())
    assert ok and error is None
    ok, error = payload_is_valid(rendered(), items(count=9))
    assert not ok and "9" in error


# --- 8-15. the ownership policy ------------------------------------------------------

def policy(**overrides):
    args = {"mode": CANARY_NEW_ONLY, "render_status": "RENDERED", "qa_status": "COMPLETED",
            "payload_valid": True, "target_exists": False, "coded_owned": False,
            "fingerprint_matches": False}
    args.update(overrides)
    return plan_decision(**args)


def test_a_missing_target_would_be_inserted():
    assert policy() == WOULD_INSERT


@pytest.mark.parametrize("mode", [DRY_RUN, CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY,
                                  EXPLICIT_BACKFILL])
def test_an_existing_legacy_owned_row_is_protected_in_every_mode(mode):
    assert policy(mode=mode, target_exists=True, coded_owned=False) \
        == PROTECTED_EXISTING_LEGACY_ROW


def test_a_legacy_owned_row_stays_protected_even_with_the_backfill_flag():
    assert plan_decision(mode=EXPLICIT_BACKFILL, render_status="RENDERED",
                         qa_status="COMPLETED", payload_valid=True, target_exists=True,
                         coded_owned=False, fingerprint_matches=False,
                         allow_update_existing=True) == PROTECTED_EXISTING_LEGACY_ROW


def test_a_coded_owned_row_with_the_same_fingerprint_is_a_noop():
    assert policy(target_exists=True, coded_owned=True,
                  fingerprint_matches=True) == WOULD_SKIP_IDENTICAL


def test_a_coded_owned_row_with_a_changed_fingerprint_may_be_updated():
    assert policy(target_exists=True, coded_owned=True,
                  fingerprint_matches=False) == WOULD_UPDATE


def test_backfill_requires_the_explicit_allow_update_flag():
    assert plan_decision(mode=EXPLICIT_BACKFILL, render_status="RENDERED",
                         qa_status="COMPLETED", payload_valid=True, target_exists=True,
                         coded_owned=True, fingerprint_matches=False,
                         allow_update_existing=False) == BLOCKED_BACKFILL_NOT_AUTHORISED
    assert plan_decision(mode=EXPLICIT_BACKFILL, render_status="RENDERED",
                         qa_status="COMPLETED", payload_valid=True, target_exists=True,
                         coded_owned=True, fingerprint_matches=False,
                         allow_update_existing=True) == WOULD_UPDATE


# --- 29-31. readiness gating ------------------------------------------------------------

@pytest.mark.parametrize("qa_status", ["PENDING", "MODEL_ERROR", "INVALID_STRUCTURED_OUTPUT",
                                       "INVALID_EVIDENCE", "REVIEW_REQUIRED"])
def test_a_non_final_evaluation_is_blocked(qa_status):
    assert policy(qa_status=qa_status) == BLOCKED_NOT_READY


@pytest.mark.parametrize("render_status", ["SOURCE_QA_NOT_READY", "INCONSISTENT_EVIDENCE"])
def test_a_non_final_render_is_blocked(render_status):
    assert policy(render_status=render_status) == BLOCKED_NOT_READY


def test_a_finalized_non_delivered_payload_is_eligible():
    assert policy(render_status="RENDERED_NON_DELIVERED",
                  qa_status="NON_DELIVERED") == WOULD_INSERT


def test_an_invalid_payload_is_blocked_before_anything_else():
    assert policy(payload_valid=False) == BLOCKED_INVALID_PAYLOAD


# --- 20-21. digests and diffs ---------------------------------------------------------

def test_digest_is_deterministic_and_order_independent():
    session = session_row(rendered())
    rows = checklist_rows(items(), "TRANSCRIPT-1")
    assert digest(session, rows) == digest(session, list(reversed(rows)))
    assert len(digest(session, rows)) == 64


def test_digest_changes_when_any_written_value_changes():
    session = session_row(rendered())
    rows = checklist_rows(items(), "TRANSCRIPT-1")
    base = digest(session, rows)
    assert digest(session_row(rendered(met_count=9)), rows) != base
    changed = checklist_rows(items(statuses={4: "Not Met"}), "TRANSCRIPT-1")
    assert digest(session, changed) != base
    assert digest(None, []) != base


def test_digest_ignores_columns_this_writer_does_not_own():
    session = session_row(rendered())
    rows = checklist_rows(items(), "TRANSCRIPT-1")
    polluted = dict(session, recording_url="https://example.invalid/x", clips_status="done")
    assert digest(polluted, rows) == digest(session, rows)


def test_numeric_formatting_is_not_reported_as_a_difference():
    proposed = session_row(rendered(engagement=Decimal("100.00")))
    existing = dict(proposed, Engagement=Decimal("100"))
    assert diff_session(proposed, existing) == {}


def test_diff_reports_only_changed_mapped_columns():
    proposed = session_row(rendered())
    existing = dict(proposed, met_count=9, recording_url="ignored")
    difference = diff_session(proposed, existing)
    assert list(difference) == ["met_count"]


def test_diff_against_a_missing_row_lists_every_column():
    assert set(diff_session(session_row(rendered()), None)) == set(SESSION_COLUMNS)


def test_checklist_diff_separates_status_and_evidence():
    proposed = checklist_rows(items(statuses={2: "Met"}), "TRANSCRIPT-1")
    existing = [dict(row) for row in checklist_rows(items(statuses={2: "Not Met"}),
                                                    "TRANSCRIPT-1")]
    existing[5]["evidence"] = "something else"
    difference = diff_checklist(proposed, existing)
    assert difference["status_difference_orders"] == [2]
    assert difference["evidence_difference_orders"] == [6]
    assert difference["missing_orders"] == []


# --- 26-28. approved policies ------------------------------------------------------------

def test_a_new_write_carries_the_corrected_item2_value():
    # The renderer's Item 2 is whatever Phase 3A decided; the writer never
    # rewrites it back to the legacy value.
    rows = checklist_rows(items(statuses={2: "Met"}), "TRANSCRIPT-1")
    assert rows[1]["status"] == "Met"
    assert "item2" not in " ".join(SESSION_COLUMNS).lower()


def test_lms_values_come_from_the_rendered_snapshot_not_a_live_query():
    row = session_row(rendered())
    assert row["lms_students"] == {"students": [{"ID": 1, "FullName": "Avery Holt"}]}
    assert row["lms_students_count"] == 2
    import inspect
    from app.writer import mapping, service
    for module in (mapping, service):
        assert "kbc_users_data" not in inspect.getsource(module)
        assert "kbc_attendance" not in inspect.getsource(module)


# --- 32-36. bounded model generations --------------------------------------------------

class StubAttempts:
    def __init__(self, existing=0):
        self.rows = []
        self.existing = existing

    def count(self, connection, fingerprint):
        return self.existing + len(self.rows)

    def record(self, connection, entry):
        self.rows.append(entry)
        return len(self.rows)


def test_the_generation_cap_is_three():
    from app.qa.service import MAX_MODEL_GENERATIONS
    assert MAX_MODEL_GENERATIONS == 3


def test_an_unsuccessful_generation_is_recorded_and_the_third_closes_the_fingerprint():
    from app.qa import service as qa_service

    class Recorder(qa_service.ShadowQaService):
        def __init__(self, attempts, outcome):
            self.attempt_repository = attempts
            self.max_model_generations = qa_service.MAX_MODEL_GENERATIONS
            self.engine_version = "engine"
            self.model_name = "gpt-5.2"
            self.marked = []
            self._outcome = outcome

        def _mark_review_required(self, connection, package, fingerprint, attempts):
            self.marked.append(attempts)
            return uuid.uuid4()

    package = {"lecture_id": uuid.uuid4()}
    for attempts_before, expect_closed in ((0, False), (1, False), (2, True)):
        attempts = StubAttempts()
        outcome = {"ai_called": True, "qa_status": "INVALID_EVIDENCE",
                   "invalid_evidence_clip_count": 2}
        service = Recorder(attempts, outcome)
        service._record_attempt(None, package, "f" * 64, outcome, attempts_before, False)
        assert len(attempts.rows) == 1
        assert attempts.rows[0]["outcome"] == "INVALID_EVIDENCE"
        assert attempts.rows[0]["metadata"]["generation_number"] == attempts_before + 1
        assert bool(service.marked) is expect_closed
        if expect_closed:
            assert outcome["qa_status"] == "REVIEW_REQUIRED"
            assert outcome["review_reason"] == "MAX_GENERATIONS_EXHAUSTED"


def test_a_successful_generation_never_closes_the_fingerprint():
    from app.qa import service as qa_service

    class Recorder(qa_service.ShadowQaService):
        def __init__(self, attempts):
            self.attempt_repository = attempts
            self.max_model_generations = 3
            self.engine_version = "engine"
            self.model_name = "gpt-5.2"
            self.marked = []

        def _mark_review_required(self, connection, package, fingerprint, attempts):
            self.marked.append(attempts)
            return None

    attempts = StubAttempts()
    outcome = {"ai_called": True, "qa_status": "COMPLETED"}
    service = Recorder(attempts)
    service._record_attempt(None, {"lecture_id": uuid.uuid4()}, "f" * 64, outcome, 2, False)
    assert service.marked == []
    assert outcome["qa_status"] == "COMPLETED"


def test_a_reused_or_uncalled_evaluation_records_no_attempt():
    from app.qa import service as qa_service

    class Recorder(qa_service.ShadowQaService):
        def __init__(self, attempts):
            self.attempt_repository = attempts
            self.max_model_generations = 3
            self.engine_version = "e"
            self.model_name = "m"

    attempts = StubAttempts()
    Recorder(attempts)._record_attempt(None, {"lecture_id": None}, "f" * 64,
                                       {"ai_called": False, "qa_status": "COMPLETED"}, 0, False)
    assert attempts.rows == []


# --- 37-40. the writer touches nothing else ----------------------------------------------

def test_the_writer_package_calls_no_model_and_no_external_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    sources = [*(root / "app" / "writer").glob("*.py"),
               root / "app" / "db" / "repositories" / "qa_writer.py"]
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("openai", "complete_json", "graph.microsoft.com", "kbc_users_data",
                          "kbc_attendance", "aptem_auto_extracting", "ffmpeg", "sharepoint"):
            assert forbidden not in text, (path.name, forbidden)


def test_the_qa_session_writer_still_never_names_the_perfect_lecture_table():
    """
    Phase 3C2.3D added a Perfect Lecture output, so `qa_perfect_lectures` is
    no longer forbidden everywhere. It IS still forbidden in the QA session
    path: only the three dedicated perfect_* modules may name that table, so
    the mapping stays in one place and a QA write can never reach it by
    accident.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    allowed = {"perfect_mapping.py", "perfect_service.py"}
    for path in [*(root / "app" / "writer").glob("*.py"),
                 root / "app" / "db" / "repositories" / "qa_writer.py"]:
        if path.name in allowed:
            continue
        assert "qa_perfect_lectures" not in path.read_text(encoding="utf-8"), path.name
    # And the repository that does own it is the only other place.
    owner = (root / "app" / "db" / "repositories" / "perfect_lectures.py")
    assert "qa_perfect_lectures" in owner.read_text(encoding="utf-8")


def test_the_perfect_lecture_path_also_calls_no_model_and_no_external_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    for path in (root / "app" / "qa" / "perfect.py",
                 root / "app" / "writer" / "perfect_mapping.py",
                 root / "app" / "writer" / "perfect_service.py",
                 root / "app" / "db" / "repositories" / "perfect_lectures.py"):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("openai", "complete_json", "graph.microsoft.com",
                          "kbc_users_data", "kbc_attendance", "aptem_auto_extracting",
                          "ffmpeg", "sharepoint", "lecture_transcript_cues"):
            assert forbidden not in text, (path.name, forbidden)


def test_only_the_two_legacy_tables_can_be_targeted_and_only_by_mapped_columns():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[2]
            / "app" / "db" / "repositories" / "qa_writer.py").read_text(encoding="utf-8")
    assert "qa_doctors_sessions" in text and "qa_doctors_checklist_items" in text
    assert "qa_doctors_transcripts" not in text
    assert "qa_lecture_split_plans" not in text
    # Every legacy write path demands proof of a write-enabled mode.
    assert text.count("allow_write") >= 6


# --- Phase 3C2: the human-safe canary guard ------------------------------------------------

def _writer(**kwargs):
    from app.writer.service import LegacyQaWriter
    return LegacyQaWriter(payload_repository=None, legacy_repository=None,
                          ownership_repository=None, **kwargs)


def test_a_write_enabled_mode_refuses_without_a_lecture_scope():
    from app.writer.modes import CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY, WriterModeError
    for mode in (CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY):
        with pytest.raises(WriterModeError, match="explicit lecture scope"):
            _writer(mode=mode, confirmed=True)


def test_a_write_enabled_mode_refuses_without_an_explicit_confirmation():
    from app.writer.modes import CANARY_NEW_ONLY, WriterModeError
    with pytest.raises(WriterModeError, match="write confirmation"):
        _writer(mode=CANARY_NEW_ONLY, lecture_ids=["a"])


def test_canary_refuses_more_than_one_lecture():
    from app.writer.modes import CANARY_NEW_ONLY, WriterModeError
    with pytest.raises(WriterModeError, match="exactly one lecture"):
        _writer(mode=CANARY_NEW_ONLY, lecture_ids=["a", "b"], confirmed=True)


def test_canary_accepts_exactly_one_confirmed_lecture():
    from app.writer.modes import CANARY_NEW_ONLY
    writer = _writer(mode=CANARY_NEW_ONLY, lecture_ids=["a"], confirmed=True)
    assert writer.writes_enabled is True
    assert writer.lecture_ids == {"a"}


def test_planning_modes_need_no_confirmation_and_never_write():
    from app.writer.modes import DISABLED, DRY_RUN
    for mode in (DISABLED, DRY_RUN):
        assert _writer(mode=mode).writes_enabled is False


def test_the_lecture_scope_is_normalized_to_text_so_uuids_match():
    # The payload repository returns UUID objects; a CLI scope arrives as text.
    from app.writer.modes import CANARY_NEW_ONLY
    identifier = uuid.uuid4()
    writer = _writer(mode=CANARY_NEW_ONLY, lecture_ids=[identifier], confirmed=True)
    assert str(identifier) in writer.lecture_ids


def test_cli_guard_refuses_unscoped_unconfirmed_and_multi_lecture_writes():
    import argparse
    from app.cli.main import guard_write_command
    from app.common.errors import PlatformError
    from app.writer.modes import CANARY_NEW_ONLY, DRY_RUN

    def args(**kwargs):
        return argparse.Namespace(**{"mode": CANARY_NEW_ONLY, "lecture_ids": None,
                                     "confirm_write": False, **kwargs})

    # A planning mode passes straight through.
    guard_write_command(args(mode=DRY_RUN))
    for kwargs, expected in (
            ({}, "date-wide write is refused"),
            ({"lecture_ids": ["a"]}, "requires --confirm-write"),
            ({"lecture_ids": ["a", "b"], "confirm_write": True}, "exactly one lecture")):
        with pytest.raises(PlatformError, match=expected):
            guard_write_command(args(**kwargs))
    # Scoped, single and confirmed is allowed.
    guard_write_command(args(lecture_ids=["a"], confirm_write=True))


def test_qa_and_render_services_expose_the_same_lecture_scope():
    # A controlled single-lecture run must not spend a provider call elsewhere.
    import inspect
    from app.qa.service import ShadowQaService
    from app.rendering.service import QaRenderingService
    for service in (ShadowQaService, QaRenderingService):
        assert "lecture_ids" in inspect.signature(service.__init__).parameters
