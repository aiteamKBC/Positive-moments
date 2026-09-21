"""
Phase 3C3D: read attendance source coverage for ONE lecture.

Deliberately small and read-only. The coverage status is DERIVED from counts
`public.lecture_attendance_snapshots` already froze, so:

  * no external attendance table is queried - `public.kbc_attendance` is owned
    by another system and this platform never reads it outside Phase 2C3;
  * no schema is added - the counts are existing columns and the derived
    status is persisted into existing jsonb metadata by the callers;
  * historical lectures answer the same question without being rewritten.

The newest snapshot for the requested resolution version wins, with the id
breaking ties, so the answer never depends on row order.
"""
from app.attendance.coverage import SOURCE_UNKNOWN, classify, provenance
from app.common.errors import DATABASE_ERROR, PlatformError


LOAD_CURRENT_SNAPSHOT = """
SELECT sn.snapshot_id, sn.source_row_count, sn.present_row_count,
       sn.effective_member_count, sn.attendance_resolution_version, sn.created_at,
       (sn.metadata ->> 'source_rows_any_status')::int
  FROM public.lecture_attendance_snapshots sn
 WHERE sn.lecture_id = %s
   AND sn.attendance_resolution_version = %s
 ORDER BY sn.created_at DESC, sn.snapshot_id DESC
 LIMIT 1
"""


class AttendanceCoverageRepository:
    def for_lecture(self, connection, lecture_id, *, attendance_resolution_version) -> dict:
        try:
            row = connection.execute(
                LOAD_CURRENT_SNAPSHOT,
                (lecture_id, attendance_resolution_version)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "attendance coverage read failed") from exc
        if row is None:
            # No snapshot at all is NOT "no attendance": it is "not yet
            # resolved", and it must never look like a confirmed zero either.
            return {**provenance(status=SOURCE_UNKNOWN),
                    "attendance_resolution_version": attendance_resolution_version,
                    "snapshot_present": False}
        # Absent on snapshots frozen before Phase 3C3E, which is why it is
        # optional: without it an empty roster reads as SOURCE_MISSING, which
        # under-claims what the source said rather than over-claiming it.
        any_status = row[6]
        status = classify(source_row_count=row[1], present_row_count=row[2],
                          effective_member_count=row[3],
                          source_rows_any_status=any_status)
        return {**provenance(status=status, source_row_count=row[1],
                             present_row_count=row[2], effective_member_count=row[3],
                             snapshot_id=row[0], source_rows_any_status=any_status),
                "attendance_resolution_version": row[4],
                "snapshot_present": True}
