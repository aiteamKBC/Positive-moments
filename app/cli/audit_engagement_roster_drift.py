"""
READ-ONLY DIAGNOSTIC - not part of the normal Phase 2C4 calculation.

Explains engagement parity gaps caused by the live attendance table changing
AFTER the legacy QA run. For each current engagement result it looks up the
live `kbc_attendance` rows behind the frozen snapshot members (by learner id,
lecture date and normalized module) and asks one question: if members whose source row is now
flagged `attendance_status = 'makeup'` are left out, does the persisted
evidence reproduce the legacy numbers?

It never writes, never changes a snapshot and never alters an engagement row.
It runs in a PostgreSQL-enforced read-only transaction and prints counts,
statuses and dates only - no names, no emails.

    python -m app.cli.audit_engagement_roster_drift --date 2026-09-04
"""
import argparse
import json
from datetime import date

from app.config.settings import Settings
from app.db.connection import readonly_database_connection
from app.db.repositories.engagement import (
    EngagementInputRepository,
    LegacyEngagementParityRepository,
)
from app.engagement.calculator import calculate
from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_ROSTER_V1, ROSTER_RULES
from app.lectures.matching import normalize_group


# "ID" is a per-learner identifier that repeats across dates, so the live
# lookup is scoped to the same lecture date and normalized module the snapshot
# was built from.
LIVE_STATUS = """
SELECT a."ID"::text, a.attendance_status, a.resolved_at, a.activity
  FROM public.kbc_attendance a
 WHERE a."ID"::text = ANY(%s)
   AND a.date = %s
   AND btrim(regexp_replace(lower(a.module), '\\s+', ' ', 'g')) = %s
   AND a."Attendance" = 1
"""

SNAPSHOT_SCOPE = """
SELECT session_date, module_normalized
  FROM public.lecture_attendance_snapshots WHERE snapshot_id = %s
"""


def audit(connection, target_date, roster_version=ATTENDANCE_ROSTER_V1) -> dict:
    inputs = EngagementInputRepository()
    snapshots = inputs.load_snapshots(
        connection, target_date,
        attendance_resolution_version=roster_version,
        resolver_version=RESOLVER_VERSION, role_algorithm_version=ROLE_ALGORITHM_VERSION)
    ids = [item["snapshot_id"] for item in snapshots]
    members = inputs.load_members(connection, ids)
    speakers = inputs.load_speakers(connection, ids, resolver_version=RESOLVER_VERSION,
                                    role_algorithm_version=ROLE_ALGORITHM_VERSION)
    legacy = {normalize_group(row["subject"]): row
              for row in LegacyEngagementParityRepository().load(connection, target_date)}

    rows = []
    matched = {"engagement": 0, "score": 0, "item7": 0, "attended": 0}
    for item in snapshots:
        roster = members.get(item["snapshot_id"], [])
        external_ids = [m["external_person_id"] for m in roster if m["external_person_id"]]
        session_date, module_normalized = connection.execute(
            SNAPSHOT_SCOPE, (item["snapshot_id"],)).fetchone()
        live = {row[0]: row for row in connection.execute(
            LIVE_STATUS, (external_ids, session_date, module_normalized)).fetchall()}
        makeup = [m for m in roster
                  if live.get(m["external_person_id"], (None, None))[1] == "makeup"]
        makeup_ids = {m["member_id"] for m in makeup}
        adjusted = calculate(
            members=[m for m in roster if m["member_id"] not in makeup_ids],
            speakers=speakers.get((item["snapshot_id"], item["document_id"]), []))
        spoke_makeup = sum(1 for p in adjusted["participants"] if p["member_id"] in makeup_ids)
        resolved_dates = sorted({live[m["external_person_id"]][2].date().isoformat()
                                 for m in makeup if live[m["external_person_id"]][2]})
        old = legacy.get(normalize_group(item["subject"]))
        row = {
            "subject": item["subject"],
            "snapshot_members": len(roster),
            "live_makeup_members": len(makeup),
            "makeup_members_resolved_on": resolved_dates,
            "makeup_members_max_live_activity": (
                str(max(live[m["external_person_id"]][3] or 0 for m in makeup)) if makeup else None),
            "makeup_members_who_spoke": spoke_makeup,
            "adjusted_attended": adjusted["attended_count"],
            "adjusted_percentage": str(adjusted["engagement_percentage"]),
            "adjusted_score": adjusted["engagement_score"],
            "adjusted_item7": adjusted["learner_engagement_status"],
        }
        if old:
            flags = {
                "engagement": adjusted["engagement_percentage"] == old["engagement"],
                "score": adjusted["engagement_score"] == old["engagement_score"],
                "item7": adjusted["learner_engagement_status"] == old["item7_status"],
                "attended": adjusted["attended_count"] == old["legacy_attended_count"],
            }
            for key, value in flags.items():
                matched[key] += value
            row["legacy_parity_if_makeup_excluded"] = {
                key: "YES" if value else "NO" for key, value in flags.items()}
        else:
            row["legacy_parity_if_makeup_excluded"] = "NO_LEGACY_ROW"
        rows.append(row)

    count = len(legacy)
    return {
        "target_date": target_date.isoformat(),
        "mode": "READ_ONLY_DIAGNOSTIC",
        "roster_version": roster_version,
        "writes": 0,
        "hypothesis": "live makeup rows were flagged after the legacy QA run",
        "parity_if_makeup_excluded": {key: f"{value} / {count}" for key, value in matched.items()},
        "lectures": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="audit-engagement-roster-drift")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    # Defaults to v1: the rule whose post-lecture drift this tool explains.
    parser.add_argument("--roster-version", default=ATTENDANCE_ROSTER_V1,
                        choices=sorted(ROSTER_RULES))
    args = parser.parse_args(argv)
    settings = Settings.from_environment()
    settings.require_database()
    with readonly_database_connection(settings.database_url) as connection:
        print(json.dumps(audit(connection, args.date, args.roster_version),
                         indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
