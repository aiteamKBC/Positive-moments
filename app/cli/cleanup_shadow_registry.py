"""
One-off Phase-1 corrective cleanup of non-canonical shadow registry rows.

Background: earlier shadow runs persisted EVERY eligible Teams calendar event
into public.lecture_sessions. The canonical registry must contain only
occurrences that exactly match an active Aptem group, so the unmatched rows are
shadow-validation artifacts and must be removed.

Safety: it touches only public.lecture_sessions, only rows whose
group_match_status is not 'MATCHED', only rows with no meeting ID, and only
after proving that no table in the database references those lecture IDs. It
reports before it writes, and --apply is required to commit. No legacy
production table is read for anything but the reference proof, and none is
written.
"""

import argparse
import json

import psycopg
from psycopg import sql

from app.config.settings import Settings


def _candidates(connection) -> list[dict]:
    rows = connection.execute("""
        SELECT lecture_id, session_date, scheduled_start, subject,
               group_match_status, calendar_mapping_status, discovery_status, meeting_id
          FROM public.lecture_sessions
         WHERE group_match_status <> 'MATCHED'
         ORDER BY session_date, scheduled_start, subject
    """).fetchall()
    return [
        {"lecture_id": str(row[0]), "session_date": row[1].isoformat(),
         "scheduled_start": row[2].isoformat(), "subject": row[3],
         "group_match_status": row[4], "calendar_mapping_status": row[5],
         "discovery_status": row[6], "meeting_id": row[7]}
        for row in rows
    ]


def _referencing_columns(connection, lecture_ids: list[str]) -> list[dict]:
    """Every uuid/text column in public, outside lecture_sessions, holding one of these IDs."""
    found: list[dict] = []
    columns = connection.execute("""
        SELECT c.table_name, c.column_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public'
           AND t.table_type = 'BASE TABLE'
           AND c.table_name <> 'lecture_sessions'
           AND c.data_type IN ('uuid', 'text', 'character varying')
    """).fetchall()
    # Some legacy columns in this database contain '%' in their names, so the
    # whole statement is composed and no client-side placeholder is used.
    wanted = sql.Literal(list(lecture_ids))
    for table, column in columns:
        statement = sql.SQL("SELECT count(*) FROM public.{} WHERE {}::text = ANY({})").format(
            sql.Identifier(table), sql.Identifier(column), wanted
        )
        count = connection.execute(statement).fetchone()[0]
        if count:
            found.append({"table": table, "column": column, "rows": count})
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cleanup-shadow-registry")
    parser.add_argument("--apply", action="store_true", help="commit the deletion (otherwise report only)")
    args = parser.parse_args(argv)

    settings = Settings.from_environment()
    settings.require_database()

    connection = psycopg.connect(settings.database_url)
    try:
        rows = _candidates(connection)
        ids = [row["lecture_id"] for row in rows]
        report = {
            "mode": "APPLY" if args.apply else "REPORT_ONLY",
            "table": "public.lecture_sessions",
            "registry_rows_before": connection.execute(
                "SELECT count(*) FROM public.lecture_sessions").fetchone()[0],
            "non_canonical_rows": rows,
            "non_canonical_count": len(rows),
            "rows_with_meeting_id": [row for row in rows if row["meeting_id"]],
            "referencing_columns": _referencing_columns(connection, ids) if ids else [],
        }
        blockers = []
        if report["rows_with_meeting_id"]:
            blockers.append("a non-canonical row carries a meeting ID")
        if report["referencing_columns"]:
            blockers.append("another table references a non-canonical lecture ID")
        report["blockers"] = blockers

        if args.apply and not blockers and ids:
            deleted = connection.execute(
                "DELETE FROM public.lecture_sessions WHERE lecture_id = ANY(%s) "
                "AND group_match_status <> 'MATCHED' AND meeting_id IS NULL",
                (ids,),
            ).rowcount
            report["deleted"] = deleted
            report["registry_rows_after"] = connection.execute(
                "SELECT count(*) FROM public.lecture_sessions").fetchone()[0]
            connection.commit()
            report["committed"] = True
        else:
            connection.rollback()
            report["deleted"] = 0
            report["committed"] = False
        print(json.dumps(report, indent=2))
        return 1 if blockers else 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
