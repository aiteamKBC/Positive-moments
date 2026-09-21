"""Read-only Phase 1 schema and shadow-evidence report."""

import json

from app.config.settings import Settings
from app.db.connection import readonly_database_connection


def main() -> int:
    settings = Settings.from_environment()
    settings.require_database()
    target_date = "2026-09-04"
    with readonly_database_connection(settings.database_url) as connection:
        columns = connection.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name IN ('lecture_sessions', 'lecture_discovery_runs')
             ORDER BY table_name, ordinal_position
            """
        ).fetchall()
        qa = connection.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE meeting_id IS NOT NULL),
                   count(DISTINCT meeting_id) FILTER (WHERE meeting_id IS NOT NULL),
                   count(DISTINCT lower(regexp_replace(btrim(subject), '\\s+', ' ', 'g')))
                       FILTER (WHERE subject IS NOT NULL)
              FROM public.qa_doctors_sessions
             WHERE date = %s
            """,
            (target_date,),
        ).fetchone()
        registry_rows = connection.execute(
            "SELECT count(*) FROM public.lecture_sessions WHERE session_date = %s", (target_date,)
        ).fetchone()[0]
        discovery_runs = connection.execute(
            "SELECT count(*) FROM public.lecture_discovery_runs WHERE target_date = %s", (target_date,)
        ).fetchone()[0]
    aptem = {
        "role": "APTEM_SOURCE_DATABASE",
        "configured": bool(settings.aptem_database_url),
        "connection_succeeded": False,
        "table_exists": None,
        "sql_execution_status": "NOT_RUN",
        "rows_before_active_filter": None,
        "active_groups_after_filter": None,
    }
    if settings.aptem_database_url:
        try:
            with readonly_database_connection(settings.aptem_database_url) as connection:
                aptem["connection_succeeded"] = True
                aptem["table_exists"] = connection.execute(
                    "SELECT to_regclass('public.aptem_auto_extracting') IS NOT NULL"
                ).fetchone()[0]
                if aptem["table_exists"]:
                    aptem["rows_before_active_filter"] = connection.execute(
                        "SELECT count(*) FROM public.aptem_auto_extracting"
                    ).fetchone()[0]
                    aptem["active_groups_after_filter"] = connection.execute(
                        '''
                        SELECT count(DISTINCT "Group")
                          FROM public.aptem_auto_extracting
                         WHERE "Group" IS NOT NULL
                           AND btrim("Group") <> ''
                           AND lower(coalesce("Program-Status", '')) = 'active'
                        '''
                    ).fetchone()[0]
                    aptem["sql_execution_status"] = "SUCCEEDED"
                else:
                    aptem["sql_execution_status"] = "TABLE_NOT_FOUND"
        except Exception as exc:
            aptem["sql_execution_status"] = "FAILED"
            aptem["error_type"] = type(exc).__name__
    report = {
        "calendar_user_configured": bool(settings.calendar_user_upn),
        "database_roles": {
            "kbc_application_database_configured": bool(settings.database_url),
            "aptem_source_database": aptem,
        },
        "columns": [
            {"table": row[0], "column": row[1], "type": row[2], "nullable": row[3]}
            for row in columns
        ],
        "counts": {
            "registry_rows_2026_09_04": registry_rows,
            "discovery_runs_2026_09_04": discovery_runs,
            "qa_rows_2026_09_04": qa[0],
            "qa_rows_with_meeting_id": qa[1],
            "qa_distinct_meeting_ids": qa[2],
            "qa_distinct_normalized_subjects": qa[3],
        },
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
