"""
Phase 2C4 persistence against the real 2026-09-04 Phase 2C3 evidence.

Every test runs inside a transaction that is rolled back. Where Phase 2C3
evidence is needed in a new shape (a changed roster, a new resolver or role
version), it is produced inside the same rolled-back transaction by the real
Phase 2C3 service, never by editing existing rows.
"""
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.attendance.service import SpeakerResolutionService
from app.config.settings import Settings
from app.db.repositories.attendance_resolution import (
    AttendanceSnapshotRepository,
    AttendanceSourceRepository,
    SpeakerIdentityRepository,
    SpeakerInventoryReadRepository,
    SpeakerResolutionRunRepository,
    SpeakerRoleRepository,
)
from app.db.repositories.engagement import (
    EngagementInputRepository,
    EngagementRepository,
    EngagementRunRepository,
    LegacyEngagementParityRepository,
)
from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
from app.engagement.service import EngagementService


TARGET = date(2026, 9, 4)

PHASE_2C3_TABLES = (
    "lecture_attendance_snapshots", "lecture_attendance_snapshot_members",
    "lecture_transcript_speaker_identities", "lecture_transcript_speaker_roles",
)
PROTECTED_TABLES = PHASE_2C3_TABLES + (
    "kbc_attendance", "lecture_sessions", "lecture_transcript_cues",
    "lecture_transcript_speakers", "lecture_transcript_documents",
    "qa_doctors_sessions", "qa_doctors_checklist_items", "qa_perfect_lectures",
    "qa_doctors_transcripts",
)


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _engagement(*, algorithm=ENGAGEMENT_ALGORITHM_VERSION, resolver=RESOLVER_VERSION,
                role=ROLE_ALGORITHM_VERSION, legacy=False):
    return EngagementService(
        input_repository=EngagementInputRepository(),
        engagement_repository=EngagementRepository(),
        run_repository=EngagementRunRepository(),
        legacy_parity_repository=LegacyEngagementParityRepository() if legacy else None,
        engagement_algorithm_version=algorithm,
        resolver_version=resolver, role_algorithm_version=role)


def _resolution(*, attendance=None, resolver=RESOLVER_VERSION, role=ROLE_ALGORITHM_VERSION):
    return SpeakerResolutionService(
        inventory_repository=SpeakerInventoryReadRepository(),
        attendance_repository=attendance or AttendanceSourceRepository(),
        snapshot_repository=AttendanceSnapshotRepository(),
        identity_repository=SpeakerIdentityRepository(),
        role_repository=SpeakerRoleRepository(),
        run_repository=SpeakerResolutionRunRepository(),
        resolver_version=resolver, role_algorithm_version=role)


class _GrownRoster(AttendanceSourceRepository):
    """Real rows plus one invented silent attendee, to simulate a roster change."""

    def load_present_rows(self, connection, session_date, module_normalized):
        rows = super().load_present_rows(connection, session_date, module_normalized)
        return rows + [{"learner_id": "-7001", "full_name": "Fixture Silentattendee",
                        "email": "fixture.silent@example.invalid",
                        "attendance_flag": 1, "module": module_normalized}]


class _Tracing:
    """Delegating proxy that records every statement."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.execute(statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.executemany(statement, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _Tracing(self._inner.cursor(*args, **kwargs), self._log)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _require_evidence(connection):
    if not connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_speaker_roles").fetchone()[0]:
        pytest.skip("Phase 2C3 evidence is not present")


def _engagement_rows(connection, version=ATTENDANCE_RESOLUTION_VERSION):
    """
    Engagement rows for ONE roster version on THIS test's target date.

    Several roster versions can be stored at once, and other lecture dates are
    now processed too, while each service run covers a single date and version -
    so comparisons are scoped rather than table-wide.
    """
    return connection.execute("""
    SELECT e.engagement_id, e.attendance_snapshot_id, e.resolver_version,
           e.role_algorithm_version, e.engagement_algorithm_version, e.calculation_status,
           e.attended_count, e.spoke_count, e.silent_count, e.engagement_percentage,
           e.engagement_score, e.learner_engagement_status, e.source_fingerprint
      FROM public.lecture_engagement_metrics e
      JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = e.attendance_snapshot_id
     WHERE s.attendance_resolution_version = %s
       AND s.session_date = %s
     ORDER BY e.engagement_id
    """, (version, TARGET)).fetchall()


# --- 32. first-run persistence ----------------------------------------------

def test_first_run_persists_one_result_per_snapshot_with_participants():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _engagement().calculate_day(connection, TARGET)
        assert summary["status"] == "COMPLETED"
        assert summary["snapshots_considered"] >= 7
        assert (summary["engagements_created"] + summary["engagements_updated"]
                == summary["snapshots_considered"])
        # One participant per snapshot member, and every member accounted for.
        orphans = connection.execute("""
        SELECT count(*) FROM public.lecture_engagement_metrics e
         WHERE (SELECT count(*) FROM public.lecture_engagement_participants p
                 WHERE p.engagement_id = e.engagement_id)
            <> (SELECT count(*) FROM public.lecture_attendance_snapshot_members m
                 WHERE m.snapshot_id = e.attendance_snapshot_id)
        """).fetchone()[0]
        assert orphans == 0
        connection.rollback()


def test_participant_counts_agree_with_the_summary_row():
    with _connection() as connection:
        _require_evidence(connection)
        _engagement().calculate_day(connection, TARGET)
        disagreements = connection.execute("""
        SELECT count(*) FROM public.lecture_engagement_metrics e
         WHERE e.spoke_count <> (SELECT count(*) FROM public.lecture_engagement_participants p
                                  WHERE p.engagement_id = e.engagement_id
                                    AND p.participation_status = 'SPOKE')
            OR e.silent_count <> (SELECT count(*) FROM public.lecture_engagement_participants p
                                   WHERE p.engagement_id = e.engagement_id
                                     AND p.participation_status = 'SILENT')
            OR e.trainer_excluded_count <> (
                   SELECT count(*) FROM public.lecture_engagement_participants p
                    WHERE p.engagement_id = e.engagement_id
                      AND p.participation_status = 'EXCLUDED_TRAINER')
        """).fetchone()[0]
        assert disagreements == 0
        connection.rollback()


def test_spoke_participants_point_at_learner_speakers_of_the_same_snapshot():
    with _connection() as connection:
        _require_evidence(connection)
        _engagement().calculate_day(connection, TARGET)
        unsafe = connection.execute("""
        SELECT count(*)
          FROM public.lecture_engagement_participants p
          JOIN public.lecture_engagement_metrics e ON e.engagement_id = p.engagement_id
          LEFT JOIN public.lecture_transcript_speaker_roles r
            ON r.speaker_id = p.first_speaker_id
           AND r.attendance_snapshot_id = e.attendance_snapshot_id
           AND r.resolver_version = e.resolver_version
           AND r.role_algorithm_version = e.role_algorithm_version
          LEFT JOIN public.lecture_transcript_speaker_identities i
            ON i.speaker_id = p.first_speaker_id
           AND i.attendance_snapshot_id = e.attendance_snapshot_id
           AND i.resolver_version = e.resolver_version
         WHERE p.participation_status = 'SPOKE'
           AND (r.role IS DISTINCT FROM 'LEARNER'
                OR i.matched_member_id IS DISTINCT FROM p.snapshot_member_id
                OR i.resolution_status NOT IN
                   ('EXACT_MATCH', 'NORMALIZED_EXACT_MATCH', 'LEGACY_FUZZY_MATCH'))
        """).fetchone()[0]
        assert unsafe == 0
        connection.rollback()


# --- 33. second-run idempotency ----------------------------------------------

def test_second_run_is_idempotent():
    with _connection() as connection:
        _require_evidence(connection)
        first = _engagement().calculate_day(connection, TARGET)
        rows_first = _engagement_rows(connection)
        participants_first = connection.execute(
            "SELECT participant_id, participation_status, matched_speaker_count "
            "  FROM public.lecture_engagement_participants ORDER BY participant_id").fetchall()

        second = _engagement().calculate_day(connection, TARGET)
        assert second["engagements_created"] == 0
        assert second["engagements_updated"] == first["snapshots_considered"]
        assert _engagement_rows(connection) == rows_first
        assert connection.execute(
            "SELECT participant_id, participation_status, matched_speaker_count "
            "  FROM public.lecture_engagement_participants ORDER BY participant_id"
        ).fetchall() == participants_first
        duplicates = connection.execute("""
        SELECT
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_engagement_metrics
             GROUP BY document_id, attendance_snapshot_id, resolver_version,
                      role_algorithm_version, engagement_algorithm_version, source_fingerprint
             HAVING count(*) > 1) a),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_engagement_participants
             GROUP BY engagement_id, snapshot_member_id HAVING count(*) > 1) b)
        """).fetchone()
        assert duplicates == (0, 0)
        connection.rollback()


# --- 28-31. provenance: source and version changes ---------------------------

def test_changed_attendance_snapshot_creates_new_engagement_provenance():
    with _connection() as connection:
        _require_evidence(connection)
        _engagement().calculate_day(connection, TARGET)
        before = _engagement_rows(connection)

        _resolution(attendance=_GrownRoster()).resolve_day(connection, TARGET)
        summary = _engagement().calculate_day(connection, TARGET)

        after = _engagement_rows(connection)
        assert set(before).issubset(set(after)), "historical results must survive untouched"
        new = [row for row in after if row not in before]
        assert len(new) == len(before)
        assert {row[1] for row in new}.isdisjoint({row[1] for row in before})
        # The invented attendee is silent, so every denominator grew by one.
        current = [row for row in summary["lectures"] if row["is_current_snapshot"]]
        assert len(current) == len(before)
        assert all(row["silent_count"] >= 1 for row in current)
        connection.rollback()


def test_new_resolver_version_creates_new_engagement_provenance():
    with _connection() as connection:
        _require_evidence(connection)
        _engagement().calculate_day(connection, TARGET)
        before = _engagement_rows(connection)
        _resolution(resolver="probe_resolver_2c4").resolve_day(connection, TARGET)
        _engagement(resolver="probe_resolver_2c4").calculate_day(connection, TARGET)
        after = _engagement_rows(connection)
        assert set(before).issubset(set(after))
        probe = [row for row in after if row[2] == "probe_resolver_2c4"]
        assert len(probe) == len(before)
        # Same evidence under a new label: same numbers, new provenance.
        assert sorted(row[6:12] for row in probe) == sorted(row[6:12] for row in before)
        connection.rollback()


def test_new_role_version_creates_new_engagement_provenance():
    with _connection() as connection:
        _require_evidence(connection)
        _engagement().calculate_day(connection, TARGET)
        before = _engagement_rows(connection)
        _resolution(role="probe_role_2c4").resolve_day(connection, TARGET)
        _engagement(role="probe_role_2c4").calculate_day(connection, TARGET)
        after = _engagement_rows(connection)
        assert set(before).issubset(set(after))
        assert len([row for row in after if row[3] == "probe_role_2c4"]) == len(before)
        connection.rollback()


def test_new_engagement_algorithm_version_creates_a_new_record():
    with _connection() as connection:
        _require_evidence(connection)
        _engagement().calculate_day(connection, TARGET)
        before = _engagement_rows(connection)
        _engagement(algorithm="probe_engagement_v2").calculate_day(connection, TARGET)
        after = _engagement_rows(connection)
        assert set(before).issubset(set(after))
        assert len([row for row in after if row[4] == "probe_engagement_v2"]) == len(before)
        connection.rollback()


# --- 34-36. sources untouched, no live attendance, no QA write ----------------

def _digest(connection):
    counts = {name: connection.execute(
        f"SELECT count(*) FROM public.{name}").fetchone()[0] for name in PROTECTED_TABLES}
    counts["roles_digest"] = connection.execute(
        "SELECT md5(string_agg(role_id::text || role || role_rank, ',' ORDER BY role_id)) "
        "  FROM public.lecture_transcript_speaker_roles").fetchone()[0]
    counts["identities_digest"] = connection.execute(
        "SELECT md5(string_agg(resolution_id::text || resolution_status || "
        "       coalesce(matched_member_id::text, ''), ',' ORDER BY resolution_id)) "
        "  FROM public.lecture_transcript_speaker_identities").fetchone()[0]
    counts["qa_engagement_digest"] = connection.execute(
        "SELECT md5(string_agg(session_id || coalesce(\"Engagement\"::text, '') || "
        "       coalesce(engagement_score::text, ''), ',' ORDER BY session_id)) "
        "  FROM public.qa_doctors_sessions").fetchone()[0]
    counts["item7_digest"] = connection.execute(
        "SELECT md5(string_agg(session_id || coalesce(status, ''), ',' ORDER BY session_id)) "
        "  FROM public.qa_doctors_checklist_items WHERE checklist_order = 7").fetchone()[0]
    return counts


def test_phase_2c3_evidence_and_qa_tables_are_unchanged():
    with _connection() as connection:
        _require_evidence(connection)
        before = _digest(connection)
        _engagement(legacy=True).calculate_day(connection, TARGET)
        _engagement(legacy=True).calculate_day(connection, TARGET)
        assert _digest(connection) == before
        connection.rollback()


def test_engagement_never_queries_live_attendance_and_never_writes_qa():
    executed: list[str] = []
    with _connection() as connection:
        _require_evidence(connection)
        _engagement(legacy=True).calculate_day(_Tracing(connection, executed), TARGET)
        connection.rollback()
    assert executed
    joined = " ".join(executed).lower()
    assert "kbc_attendance" not in joined
    assert "kbc_users_data" not in joined
    assert "aptem_auto_extracting" not in joined
    writes = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP")
    allowed = ("LECTURE_ENGAGEMENT_METRICS", "LECTURE_ENGAGEMENT_PARTICIPANTS",
               "LECTURE_ENGAGEMENT_RUNS")
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith(writes):
            assert any(table in upper for table in allowed), statement
            assert "QA_" not in upper.split("(")[0], statement


# --- 37-38. legacy parity ------------------------------------------------------

def test_legacy_parity_is_measured_and_the_algorithm_replays_exactly():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _engagement(legacy=True).calculate_day(connection, TARGET, persist=False)
        parity = summary["legacy_parity"]
        assert parity["qa_rows"] == 6
        for row in parity["rows"]:
            # The arithmetic must reproduce legacy exactly on legacy's own
            # counts; any output difference must be attributable to inputs.
            assert row["algorithm_replay_on_legacy_counts"] == "YES", row["subject"]
            if row["engagement_match"] == "NO":
                assert row["mismatch_cause"] in {
                    "ATTENDED_DENOMINATOR_DIFFERS", "SPOKE_NUMERATOR_DIFFERS",
                    "NUMERATOR_AND_DENOMINATOR_DIFFER"}
        connection.rollback()


def test_item7_parity_holds_wherever_both_inputs_agree_with_legacy():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _engagement(legacy=True).calculate_day(connection, TARGET, persist=False)
        for row in summary["legacy_parity"]["rows"]:
            if (row["spoke_new"] == row["spoke_legacy"]
                    and row["attended_new"] == row["attended_legacy"]):
                assert row["engagement_match"] == "YES", row["subject"]
                assert row["engagement_score_match"] == "YES", row["subject"]
                assert row["item7_match"] == "YES", row["subject"]
                assert row["silent_new"] == row["silent_legacy"], row["subject"]
        connection.rollback()


def test_new_platform_only_lecture_is_calculated_without_creating_qa():
    with _connection() as connection:
        _require_evidence(connection)
        qa_before = connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions").fetchone()[0]
        summary = _engagement(legacy=True).calculate_day(connection, TARGET)
        subjects = {row["subject"] for row in summary["legacy_parity"]["rows"]}
        extra = [row for row in summary["lectures"]
                 if row["is_current_snapshot"] and row["subject"] not in subjects]
        assert extra, "expected at least one lecture with no legacy QA row"
        assert all(row["calculation_status"] != "TRAINER_ATTENDANCE_AMBIGUOUS" for row in extra)
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions").fetchone()[0] == qa_before
        connection.rollback()


def test_summary_carries_no_personal_names_or_emails():
    import json
    with _connection() as connection:
        _require_evidence(connection)
        summary = _engagement(legacy=True).calculate_day(connection, TARGET)
        serialized = json.dumps(summary, default=str)
        assert "@" not in serialized
        names = {row[0] for row in connection.execute(
            "SELECT DISTINCT display_name_raw FROM public.lecture_attendance_snapshot_members"
        ).fetchall()}
        labels = {row[0] for row in connection.execute(
            "SELECT DISTINCT speaker_label_raw FROM public.lecture_transcript_speakers"
        ).fetchall()}
        for value in names | labels:
            if value and len(value.strip()) > 3:
                assert value not in serialized
        assert summary["live_attendance_queries"] == 0
        assert summary["qa_tables_written"] is False
        assert summary["ai_calls"] == 0
        assert isinstance(Decimal(summary["lectures"][0]["engagement_percentage"]), Decimal)
        connection.rollback()
