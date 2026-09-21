"""
Phase 3C3E: stamp attendance coverage onto evidence frozen before it existed.

Coverage semantics arrived in Phase 3C3D, so every row written before then
carries the counts but not the conclusion. Operations would then be reading two
models at once - a derived status on new rows, a bare `attended_count` on old
ones - and the bare zero is precisely the ambiguity the coverage model exists
to remove.

This is metadata hardening and nothing else:

  * it derives the status from counts the rows ALREADY froze. No Graph call, no
    provider call, no re-selection, no re-render, and no query of
    `public.kbc_attendance` to reconstruct what an old run saw - reconstructing
    it from today's source would be a different fact wearing an old date;
  * it never touches `qa_doctors_sessions`, `qa_doctors_checklist_items`,
    `qa_perfect_lectures`, `lecture_qa_legacy_writes` or Perfect ownership;
  * where the frozen evidence cannot support a conclusion, it writes
    `SOURCE_UNKNOWN` rather than guessing, and `SOURCE_UNKNOWN` never becomes a
    confirmed zero;
  * a second run changes nothing. Rows already carrying the current coverage
    version are skipped by the statement itself, so the NOOP is enforced in
    SQL rather than trusted in Python.
"""
import logging

from app.attendance.coverage import (
    ATTENDANCE_COVERAGE_VERSION,
    ATTENDANCE_SOURCE_MISSING,
    classify,
    is_authoritative,
)
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.common.errors import DATABASE_ERROR, PlatformError


# Snapshots that have not been stamped with the CURRENT coverage version.
SNAPSHOTS_TO_STAMP = """
SELECT sn.snapshot_id, sn.source_row_count, sn.present_row_count,
       sn.effective_member_count, (sn.metadata ->> 'source_rows_any_status')::int
  FROM public.lecture_attendance_snapshots sn
 WHERE sn.metadata ->> 'attendance_coverage_version' IS DISTINCT FROM %s
 ORDER BY sn.created_at, sn.snapshot_id
"""

# Engagement rows, with the counts of the snapshot they were calculated from.
ENGAGEMENT_TO_STAMP = """
SELECT m.engagement_id, m.calculation_status, m.attended_count,
       sn.snapshot_id, sn.source_row_count, sn.present_row_count,
       sn.effective_member_count, sn.attendance_resolution_version,
       (sn.metadata ->> 'source_rows_any_status')::int
  FROM public.lecture_engagement_metrics m
  JOIN public.lecture_attendance_snapshots sn
    ON sn.snapshot_id = m.attendance_snapshot_id
 WHERE m.metadata ->> 'attendance_coverage_version' IS DISTINCT FROM %s
 ORDER BY m.created_at, m.engagement_id
"""

STAMP_SNAPSHOT = """
UPDATE public.lecture_attendance_snapshots
   SET metadata = metadata || %s::jsonb
 WHERE snapshot_id = %s
"""

# updated_at is deliberately NOT advanced: stamping a derived conclusion onto
# frozen evidence is not a change to the evidence, and a moved timestamp would
# make "what did this run change?" unanswerable all over again.
STAMP_ENGAGEMENT = """
UPDATE public.lecture_engagement_metrics
   SET metadata = metadata || %s::jsonb
 WHERE engagement_id = %s
"""


class CoverageBackfill:
    """Derives and stamps coverage. Reads no external table."""

    def __init__(self, *, coverage_version: str = ATTENDANCE_COVERAGE_VERSION,
                 attendance_resolution_version: str = ATTENDANCE_RESOLUTION_VERSION):
        self.coverage_version = coverage_version
        self.attendance_resolution_version = attendance_resolution_version
        self.log = logging.getLogger(__name__)

    def run(self, connection, *, persist: bool = True) -> dict:
        snapshots = self._snapshots(connection, persist)
        engagement = self._engagement(connection, persist)
        distribution: dict = {}
        for bucket in (snapshots["distribution"], engagement["distribution"]):
            for status, count in bucket.items():
                distribution[status] = distribution.get(status, 0) + count
        return {
            "attendance_coverage_version": self.coverage_version,
            "mode": "BACKFILL" if persist else "DRY_RUN",
            "snapshots": snapshots,
            "engagement": engagement,
            "combined_distribution": distribution,
            "rows_stamped": snapshots["stamped"] + engagement["stamped"],
            # The boundaries, asserted as data rather than promised in prose.
            "graph_calls": 0, "provider_calls": 0,
            "external_attendance_queries": 0,
            "transcript_reads": 0, "renders": 0,
            "legacy_tables_written": 0, "perfect_tables_written": 0,
        }

    # -- stages -------------------------------------------------------------

    def _snapshots(self, connection, persist) -> dict:
        rows = self._fetch(connection, SNAPSHOTS_TO_STAMP)
        distribution: dict = {}
        stamped = 0
        for snapshot_id, source_rows, present_rows, members, any_status in rows:
            status = classify(source_row_count=source_rows,
                              present_row_count=present_rows,
                              effective_member_count=members,
                              source_rows_any_status=any_status)
            distribution[status] = distribution.get(status, 0) + 1
            if persist:
                self._stamp(connection, STAMP_SNAPSHOT, snapshot_id, {
                    "attendance_coverage_version": self.coverage_version,
                    "attendance_coverage_status": status,
                    "attendance_source_authoritative": is_authoritative(status),
                    "attendance_coverage_derived_from": "frozen_snapshot_counts",
                })
                stamped += 1
        return {"considered": len(rows), "stamped": stamped,
                "distribution": distribution}

    def _engagement(self, connection, persist) -> dict:
        rows = self._fetch(connection, ENGAGEMENT_TO_STAMP)
        distribution: dict = {}
        stamped = 0
        detail_counts: dict = {}
        for (engagement_id, calculation_status, attended, snapshot_id,
             source_rows, present_rows, members, resolution_version,
             any_status) in rows:
            status = classify(source_row_count=source_rows,
                              present_row_count=present_rows,
                              effective_member_count=members,
                              source_rows_any_status=any_status)
            distribution[status] = distribution.get(status, 0) + 1
            # The same rule the live path applies: a NO_ATTENDED_LEARNERS whose
            # source never answered is a gap, not a finding.
            detail = (ATTENDANCE_SOURCE_MISSING
                      if (calculation_status == "NO_ATTENDED_LEARNERS"
                          and not is_authoritative(status))
                      else calculation_status)
            detail_counts[detail] = detail_counts.get(detail, 0) + 1
            if persist:
                self._stamp(connection, STAMP_ENGAGEMENT, engagement_id, {
                    "attendance_coverage_version": self.coverage_version,
                    "attendance_coverage_status": status,
                    "attendance_source_authoritative": is_authoritative(status),
                    "attendance_source_row_count": source_rows,
                    "attendance_present_row_count": present_rows,
                    "attendance_effective_member_count": members,
                    "attendance_source_rows_any_status": any_status,
                    "attendance_snapshot_id": str(snapshot_id),
                    "attendance_resolution_version": resolution_version,
                    "engagement_status_detail": detail,
                    # Never derived from the count; only ever from the source.
                    "attendance_zero_confirmed": bool(
                        is_authoritative(status) and int(attended or 0) == 0),
                    "attendance_coverage_derived_from": "frozen_snapshot_counts",
                })
                stamped += 1
        return {"considered": len(rows), "stamped": stamped,
                "distribution": distribution, "status_detail": detail_counts}

    # -- plumbing -----------------------------------------------------------

    def _fetch(self, connection, statement):
        try:
            return connection.execute(statement, (self.coverage_version,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "coverage backfill read failed") from exc

    def _stamp(self, connection, statement, identifier, payload) -> None:
        import json
        try:
            connection.execute(statement, (json.dumps(payload, default=str), identifier))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "coverage backfill write failed") from exc
