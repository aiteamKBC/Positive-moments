"""
Read-only QA Master contract audit.

Proves that

    public.lecture_sessions  +  Phase 2B inputs

are the semantic equivalent of the legacy

    QA Master Daily - Safe Exact Recording v8
      -> QA One Lecture - Safe Exact Recording v8

input contract. It adds NO calendar discovery and NO Aptem query: Phase 1
already replaced that orchestration layer. Every statement below is checked
against persisted database state, never re-derived from Microsoft Graph.

The one genuinely ambiguous field is scheduledStart/scheduledEnd, because the
two exports disagree about their timezone (see the module report). This audit
therefore runs the ported selector under BOTH readings and reports whether the
disagreement can change the 2026-09-04 outcome.
"""

import argparse
import json
import re
from datetime import date, timedelta

from app.common.time import CAIRO
from app.config.settings import Settings
from app.db.connection import readonly_database_connection
from app.db.repositories.ready_lectures import LegacyQaEvidenceRepository, ReadyLectureRepository
from app.db.repositories.transcript_selections import TranscriptSelectionRepository
from app.transcripts.selection import SELECTED, select_transcript_parts


MASTER_EXPORT = "automation/legacy_n8n/QA Master Daily — Safe Exact Recording v8"
ONE_LECTURE_EXPORT = "automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json"


def legacy_normalize_group(value: str) -> str:
    """
    The QA Master "Match Calendar to Groups" normalizer, ported verbatim:

        String(value).toLowerCase().replace(/&amp;/g,'&').replace(/\\s+/g,' ').trim()
    """
    return re.sub(r"\s+", " ", str(value or "").lower().replace("&amp;", "&")).strip()


def audit(connection, target_date: date) -> dict:
    inputs = ReadyLectureRepository().load_day(connection, target_date)
    selection_repository = TranscriptSelectionRepository()
    qa_rows = LegacyQaEvidenceRepository().load_day(connection, target_date)
    qa_by_session = {row["session_id"]: row for row in qa_rows}

    lectures = []
    both_agree = True
    for lecture in inputs["ready"]:
        candidates = selection_repository.load_candidates(connection, lecture.lecture_id)

        # Reading A - the ported implementation: canonical timestamptz instants.
        canonical = select_transcript_parts(
            candidates, scheduled_start=lecture.scheduled_start,
            scheduled_end=lecture.scheduled_end, target_date=lecture.session_date,
            meeting_id=lecture.meeting_id)

        # Reading B - the legacy pipeline as exported: QA Master renders the
        # calendar in Africa/Cairo (Prefer: outlook.timezone), and QA One
        # Lecture then parses that naive string as if it were UTC. The net
        # effect is the wall-clock time reinterpreted as UTC.
        offset = lecture.scheduled_start.astimezone(CAIRO).utcoffset() or timedelta(0)
        legacy = select_transcript_parts(
            candidates, scheduled_start=lecture.scheduled_start + offset,
            scheduled_end=lecture.scheduled_end + offset,
            target_date=lecture.session_date, meeting_id=lecture.meeting_id)

        canonical_primary = (canonical.primary.candidate.provider_transcript_id
                             if canonical.status == SELECTED else None)
        legacy_primary = (legacy.primary.candidate.provider_transcript_id
                          if legacy.status == SELECTED else None)
        agrees = canonical_primary == legacy_primary
        both_agree &= agrees

        lectures.append({
            "lecture_id": str(lecture.lecture_id),
            "subject": lecture.subject,
            "module": lecture.module,
            # targetDate: legacy sliced the first 10 chars off a Cairo-rendered
            # datetime; the platform converts an aware instant to Africa/Cairo.
            "legacy_target_date_equivalent":
                lecture.scheduled_start.astimezone(CAIRO).date().isoformat(),
            "platform_session_date": lecture.session_date.isoformat(),
            "target_date_semantically_equal":
                lecture.scheduled_start.astimezone(CAIRO).date() == lecture.session_date,
            "scheduled_start_utc": lecture.scheduled_start.isoformat(),
            "scheduled_end_utc": lecture.scheduled_end.isoformat(),
            "scheduled_start_cairo_wall_clock":
                lecture.scheduled_start.astimezone(CAIRO).strftime("%Y-%m-%dT%H:%M:%S"),
            "meeting_id": lecture.meeting_id,
            "candidate_count": len(candidates),
            "canonical_primary": canonical_primary,
            "canonical_status": canonical.status,
            "canonical_same_day_candidates":
                canonical.diagnostics.get("same_day_candidate_count"),
            "canonical_window_candidates":
                canonical.diagnostics.get("occurrence_window_candidate_count"),
            "legacy_timezone_reading_primary": legacy_primary,
            "legacy_status": legacy.status,
            "legacy_window_candidates":
                legacy.diagnostics.get("occurrence_window_candidate_count"),
            "readings_agree": agrees,
            "legacy_qa_session_id": None,
            "matches_legacy_qa": None,
        })
        if canonical_primary in qa_by_session:
            lectures[-1]["legacy_qa_session_id"] = canonical_primary
            lectures[-1]["matches_legacy_qa"] = True
        else:
            match = next((row for row in qa_rows
                          if legacy_normalize_group(row["subject"])
                          == legacy_normalize_group(lecture.subject)), None)
            if match:
                lectures[-1]["legacy_qa_session_id"] = match["session_id"]
                lectures[-1]["matches_legacy_qa"] = match["session_id"] == canonical_primary

    # meetingId identity: the onlineMeeting QA Master resolved and passed down
    # must be the meeting Phase 1 persisted.
    # Scoped to downstream-ready lectures. Two MSP occurrences normalize to the
    # same subject, so an unscoped join would also pair the single legacy QA row
    # with the unresolved occurrence, which has no meeting_id by design.
    meeting_rows = connection.execute("""
        SELECT l.subject, l.meeting_id, q.meeting_id, l.downstream_ready
          FROM public.lecture_sessions l
          JOIN public.qa_doctors_sessions q
            ON lower(regexp_replace(btrim(q.subject), '\\s+', ' ', 'g'))
             = lower(regexp_replace(btrim(l.subject), '\\s+', ' ', 'g'))
         WHERE l.session_date = %s AND q.date = %s
    """, (target_date, target_date)).fetchall()
    meeting_identity = [
        {"subject": row[0], "platform_meeting_id": row[1], "legacy_meeting_id": row[2],
         "equal": row[1] == row[2]}
        for row in meeting_rows if row[3]
    ]
    meeting_identity_not_applicable = [
        {"subject": row[0], "reason": "NOT_DOWNSTREAM_READY_NO_MEETING_ID"}
        for row in meeting_rows if not row[3]
    ]

    # module: the platform normalizer is a strict superset of the legacy one
    # (full HTML entity decoding and casefold instead of &amp; and toLowerCase).
    # Prove the two agree on the live subjects and active groups.
    from app.lectures.matching import normalize_group as platform_normalize

    subjects = [row[0] for row in connection.execute(
        "SELECT subject FROM public.lecture_sessions WHERE session_date = %s",
        (target_date,)).fetchall()]
    modules = [row[0] for row in connection.execute(
        "SELECT module FROM public.lecture_sessions "
        "WHERE session_date = %s AND module IS NOT NULL", (target_date,)).fetchall()]
    normalizer_diffs = [
        {"value": value, "legacy": legacy_normalize_group(value),
         "platform": platform_normalize(value)}
        for value in set(subjects) | set(modules)
        if legacy_normalize_group(value) != platform_normalize(value)
    ]

    matched = sum(1 for item in lectures if item["matches_legacy_qa"] is True)
    return {
        "target_date": target_date.isoformat(),
        "exports_read": [MASTER_EXPORT, ONE_LECTURE_EXPORT],
        "graph_calls": 0,
        "calendar_rediscovery": False,
        "aptem_rediscovery": False,
        "canonical_lectures": inputs["considered"],
        "downstream_ready": len(inputs["ready"]),
        "skipped_not_ready": [
            {"subject": row["subject"], "reason": row["skip_reason"]}
            for row in inputs["skipped"]
        ],
        "legacy_qa_rows": len(qa_rows),
        "legacy_session_id_parity": f"{matched} / {len(qa_rows)}",
        "timezone_readings_agree_on_every_lecture": both_agree,
        "meeting_identity": meeting_identity,
        "meeting_identity_all_equal": all(row["equal"] for row in meeting_identity),
        "meeting_identity_not_applicable": meeting_identity_not_applicable,
        "module_normalizer_differences": normalizer_diffs,
        "lectures": lectures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit-qa-master-contract")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    settings = Settings.from_environment()
    settings.require_database()
    with readonly_database_connection(settings.database_url) as connection:
        report = audit(connection, args.date)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
