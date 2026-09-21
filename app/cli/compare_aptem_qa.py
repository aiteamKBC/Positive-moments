"""Read-only exact-match evidence for QA subjects versus active Aptem groups."""

import json
from datetime import date

from app.config.settings import Settings
from app.db.connection import readonly_database_connection
from app.db.repositories.aptem import AptemRepository
from app.lectures.matching import normalize_group


def compare_subjects(qa_subjects: list[str], active_groups: list[str]) -> list[dict]:
    groups_by_normalized: dict[str, list[str]] = {}
    for group in active_groups:
        groups_by_normalized.setdefault(normalize_group(group), []).append(group)
    return [
        {
            "qa_subject": subject,
            "normalized_qa_subject": normalize_group(subject),
            "exact_normalized_match": normalize_group(subject) in groups_by_normalized,
            "matching_active_groups": groups_by_normalized.get(normalize_group(subject), []),
            "reason": (
                "EXACT_NORMALIZED_ACTIVE_GROUP_MATCH"
                if normalize_group(subject) in groups_by_normalized
                else "NO_ACTIVE_APTEM_GROUP_WITH_EQUAL_NORMALIZED_TEXT"
            ),
        }
        for subject in qa_subjects
    ]


def main() -> int:
    settings = Settings.from_environment()
    settings.require_database()
    settings.require_aptem_database()
    target_date = date(2026, 9, 4)
    with readonly_database_connection(settings.database_url) as connection:
        qa_subjects = [
            row[0] for row in connection.execute(
                """
                SELECT DISTINCT subject
                  FROM public.qa_doctors_sessions
                 WHERE date = %s AND subject IS NOT NULL
                 ORDER BY subject
                """,
                (target_date,),
            ).fetchall()
        ]
    with readonly_database_connection(settings.aptem_database_url) as connection:
        active_groups = AptemRepository().load_active_groups(connection)
    print(json.dumps({
        "target_date": target_date.isoformat(),
        "qa_subject_count": len(qa_subjects),
        "active_aptem_group_count": len(active_groups),
        "comparisons": compare_subjects(qa_subjects, active_groups),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
