"""
Makeup roster drift hardening against real 2026-09-04 evidence.

Every test runs in a rolled-back transaction. Parity expectations are read
from the legacy QA rows at run time - no count is hardcoded.
"""
from datetime import date

import psycopg
import pytest

from app.attendance.roster import ATTENDANCE_ROSTER_V1, ATTENDANCE_ROSTER_V2
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
from app.engagement.service import EngagementService


TARGET = date(2026, 9, 4)


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _resolution(version):
    return SpeakerResolutionService(
        inventory_repository=SpeakerInventoryReadRepository(),
        attendance_repository=AttendanceSourceRepository(),
        snapshot_repository=AttendanceSnapshotRepository(),
        identity_repository=SpeakerIdentityRepository(),
        role_repository=SpeakerRoleRepository(),
        run_repository=SpeakerResolutionRunRepository(),
        attendance_resolution_version=version)


def _engagement(version, legacy=True):
    return EngagementService(
        input_repository=EngagementInputRepository(),
        engagement_repository=EngagementRepository(),
        run_repository=EngagementRunRepository(),
        legacy_parity_repository=LegacyEngagementParityRepository() if legacy else None,
        attendance_resolution_version=version)


def _chain(connection, version=ATTENDANCE_ROSTER_V2):
    resolution = _resolution(version).resolve_day(connection, TARGET)
    engagement = _engagement(version).calculate_day(connection, TARGET)
    return resolution, engagement


class _Tracing:
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


def _require_inventory(connection):
    if not connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_speakers").fetchone()[0]:
        pytest.skip("speaker inventory is not present")


def _v1_digest(connection):
    """Everything the v1 rule ever produced, down to timestamps."""
    return connection.execute("""
    SELECT
      (SELECT md5(string_agg(s.snapshot_id::text || s.source_fingerprint || s.created_at
                             || s.effective_member_count, ',' ORDER BY s.snapshot_id))
         FROM public.lecture_attendance_snapshots s
        WHERE s.attendance_resolution_version = %(v)s),
      (SELECT md5(string_agg(m.member_id::text || m.dedup_key, ',' ORDER BY m.member_id))
         FROM public.lecture_attendance_snapshot_members m
         JOIN public.lecture_attendance_snapshots s USING (snapshot_id)
        WHERE s.attendance_resolution_version = %(v)s),
      (SELECT md5(string_agg(i.resolution_id::text || i.resolution_status || i.updated_at,
                             ',' ORDER BY i.resolution_id))
         FROM public.lecture_transcript_speaker_identities i
         JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = i.attendance_snapshot_id
        WHERE s.attendance_resolution_version = %(v)s),
      (SELECT md5(string_agg(r.role_id::text || r.role || r.updated_at, ',' ORDER BY r.role_id))
         FROM public.lecture_transcript_speaker_roles r
         JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = r.attendance_snapshot_id
        WHERE s.attendance_resolution_version = %(v)s),
      (SELECT md5(string_agg(e.engagement_id::text || e.attended_count
                             || coalesce(e.engagement_percentage::text, '') || e.updated_at,
                             ',' ORDER BY e.engagement_id))
         FROM public.lecture_engagement_metrics e
         JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = e.attendance_snapshot_id
        WHERE s.attendance_resolution_version = %(v)s),
      (SELECT md5(string_agg(p.participant_id::text || p.participation_status,
                             ',' ORDER BY p.participant_id))
         FROM public.lecture_engagement_participants p
         JOIN public.lecture_engagement_metrics e USING (engagement_id)
         JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = e.attendance_snapshot_id
        WHERE s.attendance_resolution_version = %(v)s)
    """, {"v": ATTENDANCE_ROSTER_V1}).fetchone()


# --- 16-20. full legacy parity under the new rule ----------------------------

@pytest.fixture(scope="module")
def v2_parity():
    with _connection() as connection:
        _require_inventory(connection)
        _, engagement = _chain(connection)
        connection.rollback()
    return engagement["legacy_parity"]


@pytest.mark.parametrize("key", [
    "attended_count_parity", "spoke_count_parity", "engagement_parity",
    "engagement_score_parity", "item7_parity",
])
def test_v2_rule_reaches_full_legacy_parity(v2_parity, key):
    matched, total = (int(part) for part in v2_parity[key].split(" / "))
    assert total == v2_parity["qa_rows"] and total > 0
    assert matched == total, (key, v2_parity[key])


def test_v2_parity_rows_have_no_mismatch_cause(v2_parity):
    assert all(row["mismatch_cause"] is None for row in v2_parity["rows"])
    assert all(row["silent_new"] == row["silent_legacy"] for row in v2_parity["rows"])


# --- 14-15. engagement is snapshot-backed ------------------------------------

def test_engagement_uses_only_the_requested_roster_version():
    with _connection() as connection:
        _require_inventory(connection)
        _resolution(ATTENDANCE_ROSTER_V2).resolve_day(connection, TARGET)
        summary = _engagement(ATTENDANCE_ROSTER_V2, legacy=False).calculate_day(connection, TARGET)
        snapshot_ids = [row["attendance_snapshot_id"] for row in summary["lectures"]]
        versions = {row[0] for row in connection.execute(
            "SELECT attendance_resolution_version FROM public.lecture_attendance_snapshots "
            " WHERE snapshot_id::text = ANY(%s)", (snapshot_ids,)).fetchall()}
        assert versions == {ATTENDANCE_ROSTER_V2}
        connection.rollback()


def test_engagement_does_not_live_query_attendance_under_v2():
    executed: list[str] = []
    with _connection() as connection:
        _require_inventory(connection)
        _resolution(ATTENDANCE_ROSTER_V2).resolve_day(connection, TARGET)
        _engagement(ATTENDANCE_ROSTER_V2).calculate_day(_Tracing(connection, executed), TARGET)
        connection.rollback()
    assert executed
    assert "kbc_attendance" not in " ".join(executed).lower()


def test_v2_snapshots_contain_no_live_makeup_member():
    with _connection() as connection:
        _require_inventory(connection)
        _resolution(ATTENDANCE_ROSTER_V2).resolve_day(connection, TARGET)
        leaked = connection.execute("""
        SELECT count(*)
          FROM public.lecture_attendance_snapshot_members m
          JOIN public.lecture_attendance_snapshots s USING (snapshot_id)
          JOIN public.kbc_attendance a
            ON a."ID"::text = m.external_person_id AND a.date = s.session_date
           AND btrim(regexp_replace(lower(a.module), '\\s+', ' ', 'g')) = s.module_normalized
           AND a."Attendance" = 1
         WHERE s.attendance_resolution_version = %s AND a.attendance_status = 'makeup'
        """, (ATTENDANCE_ROSTER_V2,)).fetchone()[0]
        assert leaked == 0
        connection.rollback()


def test_v2_excluded_counts_equal_the_live_makeup_rows():
    with _connection() as connection:
        _require_inventory(connection)
        summary = _resolution(ATTENDANCE_ROSTER_V2).resolve_day(connection, TARGET)
        for lecture in summary["lectures"]:
            live = connection.execute("""
            SELECT count(*) FROM public.kbc_attendance a
             WHERE a.date = %s
               AND btrim(regexp_replace(lower(a.module), '\\s+', ' ', 'g')) = %s
               AND a."Attendance" = 1 AND a.attendance_status = 'makeup'
            """, (TARGET, lecture["module_normalized"])).fetchone()[0]
            assert lecture["excluded_makeup_count"] == live, lecture["subject"]
        connection.rollback()


# --- 21. idempotency of the whole chain --------------------------------------

def test_second_v2_chain_run_is_idempotent():
    with _connection() as connection:
        _require_inventory(connection)
        first_resolution, first_engagement = _chain(connection)
        second_resolution, second_engagement = _chain(connection)
        assert second_resolution["snapshots_created"] == 0
        assert second_resolution["snapshot_members_written"] == 0
        assert second_resolution["resolutions_created"] == 0
        assert second_resolution["roles_created"] == 0
        assert second_engagement["engagements_created"] == 0

        def shape(summary):
            return sorted(
                (row["attendance_snapshot_id"], row["engagement_id"],
                 row["source_fingerprint_prefix"], row["attended_count"], row["spoke_count"],
                 row["silent_count"], row["engagement_percentage"], row["engagement_score"],
                 row["learner_engagement_status"])
                for row in summary["lectures"])

        assert shape(first_engagement) == shape(second_engagement)
        assert (sorted((row["attendance_snapshot_id"], row["source_fingerprint_prefix"])
                       for row in first_resolution["lectures"])
                == sorted((row["attendance_snapshot_id"], row["source_fingerprint_prefix"])
                          for row in second_resolution["lectures"]))
        duplicates = connection.execute("""
        SELECT
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_attendance_snapshots
             GROUP BY lecture_id, attendance_resolution_version, source_fingerprint
             HAVING count(*) > 1) a),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_attendance_snapshot_members
             GROUP BY snapshot_id, dedup_key HAVING count(*) > 1) b),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_transcript_speaker_identities
             GROUP BY speaker_id, attendance_snapshot_id, resolver_version HAVING count(*) > 1) c),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_transcript_speaker_roles
             GROUP BY speaker_id, role_algorithm_version, resolver_version, attendance_snapshot_id
             HAVING count(*) > 1) d),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_engagement_metrics
             GROUP BY document_id, attendance_snapshot_id, resolver_version,
                      role_algorithm_version, engagement_algorithm_version, source_fingerprint
             HAVING count(*) > 1) e),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_engagement_participants
             GROUP BY engagement_id, snapshot_member_id HAVING count(*) > 1) f)
        """).fetchone()
        assert duplicates == (0, 0, 0, 0, 0, 0)
        connection.rollback()


# --- 12 (real data). trainer and learner outcomes unchanged by the rule -------

def test_v1_and_v2_agree_on_every_speaker_decision():
    with _connection() as connection:
        _require_inventory(connection)
        _resolution(ATTENDANCE_ROSTER_V1).resolve_day(connection, TARGET)
        _resolution(ATTENDANCE_ROSTER_V2).resolve_day(connection, TARGET)
        differing = connection.execute("""
        WITH decisions AS (
          SELECT s.attendance_resolution_version AS version, r.speaker_id, r.role,
                 i.resolution_status, m.external_person_id
            FROM public.lecture_transcript_speaker_roles r
            JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = r.attendance_snapshot_id
            JOIN public.lecture_transcript_speaker_identities i
              ON i.speaker_id = r.speaker_id AND i.attendance_snapshot_id = r.attendance_snapshot_id
             AND i.resolver_version = r.resolver_version
            LEFT JOIN public.lecture_attendance_snapshot_members m ON m.member_id = i.matched_member_id
            JOIN public.lecture_sessions l ON l.lecture_id = s.lecture_id
           WHERE l.session_date = %s)
        SELECT count(*) FROM decisions a JOIN decisions b
          ON a.speaker_id = b.speaker_id AND a.version = %s AND b.version = %s
         WHERE (a.role, a.resolution_status, a.external_person_id)
               IS DISTINCT FROM (b.role, b.resolution_status, b.external_person_id)
        """, (TARGET, ATTENDANCE_ROSTER_V1, ATTENDANCE_ROSTER_V2)).fetchone()[0]
        # On this date no makeup learner was a matched speaker, so the new rule
        # changes rosters only, never a speaker decision.
        assert differing == 0
        connection.rollback()


# --- 8, 22-24. old evidence and the external source are untouched -------------

def test_old_v1_rule_remains_reproducible_from_the_same_source():
    with _connection() as connection:
        _require_inventory(connection)
        existing = connection.execute(
            "SELECT count(*) FROM public.lecture_attendance_snapshots "
            " WHERE attendance_resolution_version = %s", (ATTENDANCE_ROSTER_V1,)).fetchone()[0]
        if not existing:
            pytest.skip("no persisted v1 snapshots to reproduce")
        summary = _resolution(ATTENDANCE_ROSTER_V1).resolve_day(connection, TARGET)
        # Same source + same v1 rule -> exactly the stored v1 snapshots.
        assert summary["snapshots_created"] == 0
        assert summary["snapshots_reused"] == summary["lectures_considered"]
        assert all(row["excluded_makeup_count"] == 0 for row in summary["lectures"])
        connection.rollback()


def test_v2_chain_preserves_old_snapshot_and_engagement_evidence():
    with _connection() as connection:
        _require_inventory(connection)
        before = _v1_digest(connection)
        if before[0] is None:
            pytest.skip("no persisted v1 evidence")
        _chain(connection)
        _chain(connection)
        assert _v1_digest(connection) == before
        connection.rollback()


def test_kbc_attendance_is_never_written_by_either_rule():
    executed: list[str] = []
    with _connection() as connection:
        _require_inventory(connection)
        before = connection.execute(
            "SELECT count(*), md5(string_agg(key || coalesce(attendance_status, '') "
            "       || coalesce(\"Attendance\"::text, ''), ',' ORDER BY key)) "
            "  FROM public.kbc_attendance WHERE date = %s", (TARGET,)).fetchone()
        traced = _Tracing(connection, executed)
        for version in (ATTENDANCE_ROSTER_V1, ATTENDANCE_ROSTER_V2):
            _resolution(version).resolve_day(traced, TARGET)
            _engagement(version).calculate_day(traced, TARGET)
        after = connection.execute(
            "SELECT count(*), md5(string_agg(key || coalesce(attendance_status, '') "
            "       || coalesce(\"Attendance\"::text, ''), ',' ORDER BY key)) "
            "  FROM public.kbc_attendance WHERE date = %s", (TARGET,)).fetchone()
        connection.rollback()
    assert before == after
    writes = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP", "MERGE")
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith(writes):
            assert "KBC_ATTENDANCE" not in upper, statement
