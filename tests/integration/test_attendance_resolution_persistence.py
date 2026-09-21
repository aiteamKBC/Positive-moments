"""
Phase 2C3 persistence against the real 2026-09-04 speaker inventory and the
real public.kbc_attendance rows.

Every test runs inside a transaction that is rolled back, so nothing here
leaves a row behind, no canonical evidence is touched and the external
attendance table is never written.
"""
import uuid
from datetime import date

import psycopg
import pytest

from app.attendance.resolver import MATCHED_STATUSES, RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION, TRAINER_CANDIDATE
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
from app.db.repositories.transcript_speakers import LegacyTrainerDiagnosticRepository


TARGET = date(2026, 9, 4)

PROTECTED_TABLES = (
    "kbc_attendance", "kbc_users_data",
    "lecture_transcript_cues", "lecture_transcript_speakers",
    "lecture_transcript_documents", "lecture_transcript_selections",
    "lecture_transcript_artifacts", "lecture_combined_transcripts",
    "qa_doctors_sessions", "qa_doctors_transcripts",
)


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _service(*, attendance_repository=None, resolver_version=RESOLVER_VERSION,
             role_algorithm_version=ROLE_ALGORITHM_VERSION, legacy=False):
    return SpeakerResolutionService(
        inventory_repository=SpeakerInventoryReadRepository(),
        attendance_repository=attendance_repository or AttendanceSourceRepository(),
        snapshot_repository=AttendanceSnapshotRepository(),
        identity_repository=SpeakerIdentityRepository(),
        role_repository=SpeakerRoleRepository(),
        run_repository=SpeakerResolutionRunRepository(),
        legacy_diagnostic_repository=LegacyTrainerDiagnosticRepository() if legacy else None,
        resolver_version=resolver_version,
        role_algorithm_version=role_algorithm_version,
    )


class _MutatedAttendance(AttendanceSourceRepository):
    """Real query, then one extra invented member, to simulate a roster change."""

    def load_present_rows(self, connection, session_date, module_normalized):
        rows = super().load_present_rows(connection, session_date, module_normalized)
        return rows + [{"learner_id": -991, "full_name": "Fixture Rosterchange",
                        "email": "fixture.rosterchange@example.invalid",
                        "attendance_flag": 1, "module": module_normalized}]


def _counts(connection):
    return {name: connection.execute(f"SELECT count(*) FROM public.{name}").fetchone()[0]
            for name in PROTECTED_TABLES}


# --- 27. first-run persistence ---------------------------------------------

def test_first_run_persists_snapshots_resolutions_and_roles():
    with _connection() as connection:
        summary = _service().resolve_day(connection, TARGET)
        assert summary["status"] == "COMPLETED"
        assert summary["lectures_considered"] == 7
        assert summary["speakers_considered"] > 0
        # Every speaker gets exactly one resolution and exactly one role.
        assert (summary["resolutions_created"] + summary["resolutions_updated"]
                == summary["speakers_considered"])
        assert (summary["roles_created"] + summary["roles_updated"]
                == summary["speakers_considered"])
        assert summary["snapshots_created"] + summary["snapshots_reused"] == 7
        connection.rollback()


def test_every_lecture_has_exactly_one_trainer_candidate():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        # Exactly one trainer per snapshot PER CANONICAL DOCUMENT. Several
        # roster versions may be stored, and since Phase 3C2.3B a lecture can
        # also have more than one parser version sharing a snapshot, so the
        # invariant is scoped to the document the speakers belong to rather
        # than to the snapshot alone.
        rows = connection.execute("""
        SELECT r.attendance_snapshot_id, sp.document_id, count(*)
          FROM public.lecture_transcript_speaker_roles r
          JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
         WHERE r.role = %s
         GROUP BY r.attendance_snapshot_id, sp.document_id
        """, (TRAINER_CANDIDATE,)).fetchall()
        pairs_with_roles = connection.execute("""
            SELECT count(*) FROM (
              SELECT DISTINCT r.attendance_snapshot_id, sp.document_id
                FROM public.lecture_transcript_speaker_roles r
                JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id) t
            """).fetchone()[0]
        assert len(rows) == pairs_with_roles >= 7
        assert all(row[2] == 1 for row in rows)


def test_two_parser_versions_of_one_lecture_agree_on_the_trainer():
    """A seam-deduplicated document must not change who the trainer is."""
    with _connection() as connection:
        disagreements = connection.execute("""
        SELECT d.lecture_id, count(DISTINCT sp.speaker_label_normalized)
          FROM public.lecture_transcript_speaker_roles r
          JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
          JOIN public.lecture_transcript_documents d ON d.document_id = sp.document_id
         WHERE r.role = %s
         GROUP BY d.lecture_id, r.attendance_snapshot_id
        HAVING count(DISTINCT sp.speaker_label_normalized) > 1
        """, (TRAINER_CANDIDATE,)).fetchall()
        assert disagreements == []
        connection.rollback()
        connection.rollback()


def test_trainer_candidate_is_the_highest_gross_spoken_speaker():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        mismatches = connection.execute("""
        SELECT count(*)
          FROM public.lecture_transcript_speaker_roles r
          JOIN public.lecture_transcript_speakers s ON s.speaker_id = r.speaker_id
          JOIN public.lecture_transcript_speakers peer ON peer.document_id = s.document_id
         WHERE r.role = %s AND peer.gross_spoken_ms > s.gross_spoken_ms
        """, (TRAINER_CANDIDATE,)).fetchone()[0]
        assert mismatches == 0
        connection.rollback()


def test_matched_resolutions_carry_a_member_and_ambiguous_ones_never_do():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        rows = connection.execute("""
        SELECT resolution_status, count(*),
               count(matched_member_id) AS with_member
          FROM public.lecture_transcript_speaker_identities
         GROUP BY resolution_status
        """).fetchall()
        for status, total, with_member in rows:
            if status in MATCHED_STATUSES:
                assert with_member == total, status
            else:
                assert with_member == 0, status
        connection.rollback()


def test_snapshot_members_store_no_plain_email_address():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        leaked = connection.execute("""
        SELECT count(*) FROM public.lecture_attendance_snapshot_members
         WHERE dedup_key LIKE '%%@%%' OR email_sha256 LIKE '%%@%%'
        """).fetchone()[0]
        assert leaked == 0
        connection.rollback()


# --- 28. second-run idempotency ---------------------------------------------

def test_second_run_creates_nothing_new_and_keeps_the_same_decisions():
    with _connection() as connection:
        first = _service().resolve_day(connection, TARGET)
        before = connection.execute("""
        SELECT speaker_id, attendance_snapshot_id, resolution_status,
               matched_member_id, match_method, candidate_count
          FROM public.lecture_transcript_speaker_identities
         ORDER BY speaker_id, attendance_snapshot_id, resolver_version
        """).fetchall()
        roles_before = connection.execute("""
        SELECT speaker_id, role, role_rank FROM public.lecture_transcript_speaker_roles
         ORDER BY speaker_id, attendance_snapshot_id, role_algorithm_version
        """).fetchall()

        second = _service().resolve_day(connection, TARGET)
        assert second["snapshots_created"] == 0
        assert second["snapshots_reused"] == 7
        assert second["resolutions_created"] == 0
        assert second["roles_created"] == 0
        assert second["resolutions_updated"] == first["speakers_considered"]

        after = connection.execute("""
        SELECT speaker_id, attendance_snapshot_id, resolution_status,
               matched_member_id, match_method, candidate_count
          FROM public.lecture_transcript_speaker_identities
         ORDER BY speaker_id, attendance_snapshot_id, resolver_version
        """).fetchall()
        roles_after = connection.execute("""
        SELECT speaker_id, role, role_rank FROM public.lecture_transcript_speaker_roles
         ORDER BY speaker_id, attendance_snapshot_id, role_algorithm_version
        """).fetchall()
        assert after == before
        assert roles_after == roles_before
        connection.rollback()


def test_no_duplicate_snapshots_members_resolutions_or_roles():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        _service().resolve_day(connection, TARGET)
        duplicates = connection.execute("""
        SELECT
          (SELECT count(*) FROM (
             SELECT lecture_id, attendance_resolution_version, source_fingerprint
               FROM public.lecture_attendance_snapshots
              GROUP BY 1, 2, 3 HAVING count(*) > 1) a),
          (SELECT count(*) FROM (
             SELECT snapshot_id, dedup_key
               FROM public.lecture_attendance_snapshot_members
              GROUP BY 1, 2 HAVING count(*) > 1) b),
          (SELECT count(*) FROM (
             SELECT speaker_id, attendance_snapshot_id, resolver_version
               FROM public.lecture_transcript_speaker_identities
              GROUP BY 1, 2, 3 HAVING count(*) > 1) c),
          (SELECT count(*) FROM (
             SELECT speaker_id, role_algorithm_version, resolver_version,
                    attendance_snapshot_id
               FROM public.lecture_transcript_speaker_roles
              GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) d)
        """).fetchone()
        assert duplicates == (0, 0, 0, 0)
        connection.rollback()


# --- 8 / 31. source change and version coexistence --------------------------

def test_changed_roster_adds_a_new_snapshot_without_deleting_the_old_evidence():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        original = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_attendance_snapshots").fetchall()}
        original_resolutions = connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_speaker_identities").fetchone()[0]

        changed = _service(attendance_repository=_MutatedAttendance()).resolve_day(
            connection, TARGET)

        after = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_attendance_snapshots").fetchall()}
        # Old snapshots survive untouched; the changed roster added exactly one
        # new snapshot per lecture. (Other roster versions may also be stored,
        # so the count is relative, not a doubling of the whole table.)
        assert original.issubset(after)
        assert changed["snapshots_created"] == changed["lectures_considered"]
        assert len(after - original) == changed["lectures_considered"]
        # Every original resolution is still present alongside the new ones.
        surviving = connection.execute("""
        SELECT count(*) FROM public.lecture_transcript_speaker_identities
         WHERE attendance_snapshot_id = ANY(%s)
        """, (list(original),)).fetchone()[0]
        assert surviving == original_resolutions
        connection.rollback()


def test_a_new_resolver_version_coexists_with_the_old_identity_decisions():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        before = connection.execute("""
        SELECT resolution_id, resolution_status FROM public.lecture_transcript_speaker_identities
         WHERE resolver_version = %s ORDER BY resolution_id
        """, (RESOLVER_VERSION,)).fetchall()

        probe_run = _service(resolver_version="probe_resolver_v_test").resolve_day(
            connection, TARGET)

        after = connection.execute("""
        SELECT resolution_id, resolution_status FROM public.lecture_transcript_speaker_identities
         WHERE resolver_version = %s ORDER BY resolution_id
        """, (RESOLVER_VERSION,)).fetchall()
        assert after == before
        probe = connection.execute("""
        SELECT count(*) FROM public.lecture_transcript_speaker_identities
         WHERE resolver_version = 'probe_resolver_v_test'
        """).fetchone()[0]
        assert probe == probe_run["speakers_considered"]
        connection.rollback()


def test_a_new_role_version_coexists_and_does_not_touch_person_resolution():
    with _connection() as connection:
        _service().resolve_day(connection, TARGET)
        identities_before = connection.execute("""
        SELECT resolution_id, resolution_status, matched_member_id, updated_at
          FROM public.lecture_transcript_speaker_identities ORDER BY resolution_id
        """).fetchall()
        roles_before = connection.execute("""
        SELECT role_id, role FROM public.lecture_transcript_speaker_roles
         WHERE role_algorithm_version = %s ORDER BY role_id
        """, (ROLE_ALGORITHM_VERSION,)).fetchall()

        probe_run = _service(role_algorithm_version="probe_role_v_test").resolve_day(
            connection, TARGET)

        roles_after = connection.execute("""
        SELECT role_id, role FROM public.lecture_transcript_speaker_roles
         WHERE role_algorithm_version = %s ORDER BY role_id
        """, (ROLE_ALGORITHM_VERSION,)).fetchall()
        assert roles_after == roles_before
        probe = connection.execute("""
        SELECT count(*) FROM public.lecture_transcript_speaker_roles
         WHERE role_algorithm_version = 'probe_role_v_test'
        """).fetchone()[0]
        assert probe == probe_run["speakers_considered"]
        # Person resolution rows are re-stated identically, never rewritten by
        # a role-algorithm change.
        identities_after = connection.execute("""
        SELECT resolution_id, resolution_status, matched_member_id
          FROM public.lecture_transcript_speaker_identities ORDER BY resolution_id
        """).fetchall()
        assert [row[:3] for row in identities_before] == identities_after
        connection.rollback()


# --- 29-31. sources stay untouched ------------------------------------------

def test_canonical_cues_speakers_and_external_tables_are_unchanged():
    with _connection() as connection:
        before = _counts(connection)
        speaker_digest_before = connection.execute("""
        SELECT count(*), sum(gross_spoken_ms), max(updated_at)
          FROM public.lecture_transcript_speakers
        """).fetchone()
        cue_digest_before = connection.execute(
            "SELECT count(*), sum(end_ms - start_ms) FROM public.lecture_transcript_cues"
        ).fetchone()

        _service(legacy=True).resolve_day(connection, TARGET)

        assert _counts(connection) == before
        assert connection.execute("""
        SELECT count(*), sum(gross_spoken_ms), max(updated_at)
          FROM public.lecture_transcript_speakers
        """).fetchone() == speaker_digest_before
        assert connection.execute(
            "SELECT count(*), sum(end_ms - start_ms) FROM public.lecture_transcript_cues"
        ).fetchone() == cue_digest_before
        connection.rollback()


class _TracingCursor:
    """Delegating proxy that records every statement without mutating psycopg."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.execute(statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.executemany(statement, *args, **kwargs)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _TracingConnection:
    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.execute(statement, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _TracingCursor(self._inner.cursor(*args, **kwargs), self._log)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_no_statement_writes_an_external_source_table():
    """Record every statement the phase issues and prove the sources stay read-only."""
    executed: list[str] = []
    with _connection() as connection:
        _service(legacy=True).resolve_day(_TracingConnection(connection, executed), TARGET)
        connection.rollback()

    assert executed, "no statements were captured"
    writing = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP")
    protected = ("KBC_ATTENDANCE", "KBC_USERS_DATA", "APTEM_AUTO_EXTRACTING",
                 "LECTURE_TRANSCRIPT_CUES", "LECTURE_TRANSCRIPT_SPEAKERS",
                 "LECTURE_TRANSCRIPT_DOCUMENTS", "LECTURE_SESSIONS",
                 "QA_DOCTORS_SESSIONS")
    saw_attendance_select = False
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith("SELECT") and "KBC_ATTENDANCE" in upper:
            saw_attendance_select = True
        if any(upper.startswith(verb) for verb in writing):
            for table in protected:
                assert table not in upper, statement
    assert saw_attendance_select, "the attendance source was never read"
    joined = " ".join(executed).lower()
    assert "kbc_users_data" not in joined
    assert "aptem_auto_extracting" not in joined


# --- 32 / reporting hygiene --------------------------------------------------

def test_summary_reports_no_engagement_and_no_ai_override():
    with _connection() as connection:
        summary = _service(legacy=True).resolve_day(connection, TARGET)
        assert summary["engagement_calculated"] is False
        assert summary["ai_trainer_override_applied"] is False
        assert summary["graph_calls"] == 0
        assert summary["webvtt_parsed"] is False
        assert summary["aptem_queries"] == 0
        assert summary["ai_calls"] == 0
        assert summary["deterministic_trainer_source"] == "VTT_TOP_SPEAKER"
        for key in ("engagement_percentage", "engagement_score", "spoke_count",
                    "speaker_share", "silent_students"):
            assert key not in summary
        connection.rollback()


def test_summary_never_carries_a_speaker_or_attendee_name():
    with _connection() as connection:
        summary = _service(legacy=True).resolve_day(connection, TARGET)
        labels = {row[0] for row in connection.execute(
            "SELECT DISTINCT speaker_label_raw FROM public.lecture_transcript_speakers"
        ).fetchall()}
        names = {row[0] for row in connection.execute(
            'SELECT DISTINCT "FullName" FROM public.kbc_attendance WHERE date = %s',
            (TARGET,)).fetchall()}
        import json
        serialized = json.dumps(summary, default=str)
        for value in labels | names:
            if value and len(str(value).strip()) > 3:
                assert str(value) not in serialized
        connection.rollback()


def test_legacy_trainer_parity_is_measured_read_only():
    with _connection() as connection:
        summary = _service(legacy=True).resolve_day(connection, TARGET)
        parity = summary["legacy_trainer_parity"]
        assert parity["qa_rows"] == 6
        # Measured, never forced: the assertion is that it is reported, and
        # that it cannot exceed the number of legacy rows.
        assert 0 <= parity["exact_raw_matches"] <= parity["qa_rows"]
        assert 0 <= parity["exact_normalized_matches"] <= parity["qa_rows"]
        connection.rollback()


def test_run_audit_row_records_versions_and_counts_only():
    with _connection() as connection:
        summary = _service().resolve_day(connection, TARGET)
        row = connection.execute("""
        SELECT attendance_resolution_version, resolver_version, role_algorithm_version,
               status, speakers_considered, metadata
          FROM public.lecture_speaker_resolution_runs WHERE run_id = %s
        """, (uuid.UUID(summary["run_id"]),)).fetchone()
        assert row[0] == ATTENDANCE_RESOLUTION_VERSION
        assert row[1] == RESOLVER_VERSION
        assert row[2] == ROLE_ALGORITHM_VERSION
        assert row[3] == "COMPLETED"
        assert row[4] == summary["speakers_considered"]
        assert row[5]["engagement_calculated"] is False
        connection.rollback()
