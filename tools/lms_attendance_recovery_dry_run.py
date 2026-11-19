"""
READ-ONLY dry run of LMS attendance recovery over a date range.

For every lecture whose newest attendance snapshot is not authoritative (or that has
none), it runs the REAL resolution path - SpeakerResolutionService.resolve_lecture with
persist=False and the LMS attendance source, exactly as the pipeline builds it - and
reports what a recovery would do. Nothing is written anywhere: the only connection is
tools.audit_qa_core_production.connect_read_only (read-only session, write probe
refused), client-side binding, no prepares. The LMS register is read through that same
connection's lms_sync foreign tables.

Counts only on stdout. `--details FILE` writes one CSV line per lecture with ids,
dates, decisions and counts - never a name, an email or a profile id.
`--recoverable-ids FILE` writes the ids of the lectures a recovery would now complete,
one per line, for the attendance-only batch:

    python -m tools.lms_attendance_recovery_dry_run --from 2026-07-01 --to 2026-11-19 \
        [--details out.csv] [--recoverable-ids ids.txt]
"""
from __future__ import annotations

import argparse
import collections
import csv
import sys
from datetime import date

import psycopg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, required=True)
    parser.add_argument("--details", help="optional CSV path (ids and counts only)")
    parser.add_argument("--recoverable-ids", help="optional path: recoverable lecture ids, one per line")
    args = parser.parse_args(argv)

    from app.attendance import lms_source as lms
    from app.attendance.coverage import classify, is_authoritative
    from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
    from app.attendance.service import AttendanceScopeError, SpeakerResolutionService
    from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
    from app.db.repositories.attendance_resolution import (
        AttendanceSnapshotRepository,
        AttendanceSourceRepository,
        SpeakerIdentityRepository,
        SpeakerInventoryReadRepository,
        SpeakerResolutionRunRepository,
        SpeakerRoleRepository,
    )
    from tools.audit_qa_core_production import connect_read_only

    class Memo:
        """One LMS read per lecture, although the service and the report both ask."""

        def __init__(self):
            self.source, self.cache = lms.LmsAttendanceSource(), {}

        def decide(self, connection, lecture_id):
            key = str(lecture_id)
            if key not in self.cache:
                self.cache[key] = self.source.decide(connection, lecture_id)
            return self.cache[key]

    source = Memo()
    conn, _ = connect_read_only()
    conn.prepare_threshold = None                   # pooled server: no server-side prepares
    conn.cursor_factory = psycopg.ClientCursor
    coverage = AttendanceCoverageRepository()
    service = SpeakerResolutionService(
        inventory_repository=SpeakerInventoryReadRepository(),
        attendance_repository=AttendanceSourceRepository(),
        # persist=False: none of these writers is ever called.
        snapshot_repository=AttendanceSnapshotRepository(),
        identity_repository=SpeakerIdentityRepository(),
        role_repository=SpeakerRoleRepository(), run_repository=SpeakerResolutionRunRepository(),
        lms_attendance_source=source)

    lectures = conn.execute(
        "SELECT lecture_id::text, session_date FROM public.lecture_sessions "
        " WHERE session_date BETWEEN %s AND %s ORDER BY session_date, scheduled_start, lecture_id",
        (args.date_from, args.date_to)).fetchall()

    totals, learners = collections.Counter(), collections.Counter()
    details = []
    for lecture_id, session_date in lectures:
        before = coverage.for_lecture(conn, lecture_id,
                                      attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION)
        line = {"lecture_id": lecture_id, "session_date": session_date.isoformat(),
                "coverage_before": before["attendance_coverage_status"]}
        if before["attendance_source_authoritative"]:
            outcome = "AUTHORITATIVE_KEPT"
        else:
            try:
                observed = service.resolve_lecture(conn, lecture_id, persist=False)["lectures"][0]
            except AttendanceScopeError:
                observed = None
            decision = source.decide(conn, lecture_id)
            line.update({"lms_decision": decision.status, "match_status": decision.match_status or "",
                         "reason": decision.reason or "",
                         "lms_rows_any_status": decision.source_rows_any_status,
                         "lms_learners_attended": decision.counts.get("learners_attended", "")})
            if observed is not None:
                after = classify(source_row_count=observed["attendance_source_row_count"],
                                 present_row_count=observed["attendance_present_row_count"],
                                 effective_member_count=observed["effective_attendance_count"],
                                 source_rows_any_status=observed["attendance_source_rows_any_status"])
                line.update({"source_after": observed["attendance_source"], "coverage_after": after,
                             "members_after": observed["effective_attendance_count"]})
                if is_authoritative(after):
                    outcome = ("RECOVERABLE_FROM_LMS"
                               if observed["attendance_source"] == lms.LMS_ATTENDANCE_SOURCE
                               else "RECOVERABLE_FROM_KBC")
                elif observed["attendance_source"] == lms.LMS_ATTENDANCE_SOURCE:
                    outcome = f"LMS_USED_BUT_{after}"
                else:
                    outcome = f"WAITING:{decision.status}"
            else:
                outcome = ("LMS_READY_NO_TRANSCRIPT_YET" if decision.used
                           else f"WAITING_NO_TRANSCRIPT:{decision.status}")
            totals[f"lms_decision:{decision.status}"] += 1
            totals[f"match_status:{decision.match_status or 'NONE'}"] += 1
            if decision.used and outcome == "RECOVERABLE_FROM_LMS":
                learners.update(decision.counts)
        line["outcome"] = outcome
        totals[f"outcome:{outcome}"] += 1
        details.append(line)
    conn.close()

    print(f"lectures {args.date_from}..{args.date_to}: {len(lectures)}")
    for key in sorted(totals):
        print(f"  {key}: {totals[key]}")
    print("learners behind RECOVERABLE_FROM_LMS:")
    for key in sorted(learners):
        print(f"  {key}: {learners[key]}")
    if args.recoverable_ids:
        ids = [line["lecture_id"] for line in details if line["outcome"].startswith("RECOVERABLE_FROM_")]
        with open(args.recoverable_ids, "w", encoding="utf-8") as handle:
            handle.write("".join(f"{lecture_id}\n" for lecture_id in ids))
        print(f"recoverable ids: {len(ids)} written")
    if args.details:
        fields = sorted({key for line in details for key in line})
        with open(args.details, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(details)
        print(f"details: {len(details)} lines written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
