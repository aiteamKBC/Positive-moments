"""
Historical Backfill: the QA database as a second, independent source of historical lectures (2026-11-02).

WHY
    The Teams calendar no longer answers for most historical lectures. Measured on production (READ ONLY,
    2026-11-02): qa_doctors_sessions holds 803 lectures from 2026-02-02; for February - August the calendar
    returns few or none of them (a finished course's events are removed, and its subject no longer matches an
    ACTIVE Aptem group, so even a surviving event is not canonical). A lecture the QA flow analysed is evidence
    that the lecture took place, so every day of a range reads BOTH sources, and neither is a fallback.

THE QA RECORD (public.qa_doctors_sessions, READ ONLY - never written, normalised or backfilled here)
    date        the lecture's Cairo business date (equal to lecture_sessions.session_date for every September and
                October row that has a registry lecture: 173 / 173 on production)
    meeting_id  the Graph onlineMeeting id of the Teams meeting (a SERIES: 803 rows share 79 meetings, so it
                identifies a lecture only together with its date)
    session_id  the Graph callTranscript id the legacy flow analysed (the table's primary key; not a meeting id)
    No scheduled start or end, no calendar event id.

IDENTITY (strong identifiers only; a title, trainer or module is never evidence)
    A QA record and a publisher lecture are the same lecture when they carry the SAME Teams meeting id on the
    SAME Cairo date - the join the QA directory already uses (lecture_directory.DIRECTORY). Per (date, meeting):
      * exactly one live (not duplicate-suppressed) lecture  -> the QA records belong to it (several QA records
                                                              for one lecture are duplicates: one lecture);
      * none live, a suppressed duplicate only               -> they belong to it, and it stays suppressed;
      * several live lectures                                 -> CONFLICT: kept for review, merged into none;
      * no lecture at all                                     -> QA ONLY.

QA ONLY - KEPT, NEVER INVENTED
    A QA-only lecture is shown in every preview and run with the exact evidence it lacks. It is NOT registered:
    the canonical lecture identity is the calendar occurrence (lecture_identity), and the LMS match rule
    (meeting_key_start_v2) needs the occurrence's EXACT start, which only the calendar gives - QA has neither,
    and a start taken from the LMS would make the match prove itself. So nothing is published for it; the
    reason says which of these holds (production, 2026-11-02: 599 of 615 such lectures have no LMS session at all):
      QA_ONLY_NO_LMS_SESSION     the LMS has no session of this Teams meeting on this date: nothing to publish to;
      QA_ONLY_SCHEDULE_UNKNOWN   the LMS has one, but without the calendar occurrence the lecture's exact start
                                 is unknown: an operator decision is needed;
      QA_ONLY_LMS_UNCHECKED      the LMS could not be read for the day;
      QA_ONLY_NO_MEETING         the record has no parsable Teams meeting.

Raw meeting / transcript ids never leave this module: rows carry `meeting_ref` / `qa_ref` (sha256 prefixes).
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date

from app.lms import meeting_key

QA_RECORDS = """
SELECT q.session_id, q.meeting_id, q.date, q.subject, q.lms_module, q.cancelled_session,
       -- columns other workflows added later (not on every copy of the table): read by name, absent = no reference
       (to_jsonb(q) ->> 'transcript_id' IS NOT NULL OR to_jsonb(q) ->> 'transcript_call_id' IS NOT NULL),
       to_jsonb(q) ->> 'recording_item_id' IS NOT NULL
  FROM public.qa_doctors_sessions q
 WHERE q.date BETWEEN %s AND %s
 ORDER BY q.date, q.session_id
"""
REGISTERED_MEETINGS = """
SELECT l.lecture_id::text, l.session_date, l.meeting_id, l.metadata -> 'duplicate_suppression' IS NOT NULL
  FROM public.lecture_sessions l
 WHERE l.session_date BETWEEN %s AND %s
"""

QA_ONLY = "QA_ONLY"
MATCHED, CONFLICT = "MATCHED", "CONFLICT"
NO_LMS_SESSION = "QA_ONLY_NO_LMS_SESSION"
SCHEDULE_UNKNOWN = "QA_ONLY_SCHEDULE_UNKNOWN"
LMS_UNCHECKED = "QA_ONLY_LMS_UNCHECKED"
NO_MEETING = "QA_ONLY_NO_MEETING"
# The evidence the publisher pipeline needs and a QA-only lecture does not have.
MISSING_EVIDENCE = ("TEAMS_CALENDAR_OCCURRENCE", "EXACT_SCHEDULED_START")


def ref(value) -> str | None:
    """A short, stable fingerprint of an identifier: compares rows without carrying the identifier."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else None


def load_records(connection, date_from: date, date_to: date) -> list[dict]:
    """
    The QA records of the range, READ ONLY. `meeting_key` (the LMS meeting key of the Graph meeting id) is kept for
    the LMS check of the same call; it is dropped before anything is stored (see entry()).
    """
    out = []
    for session, meeting, day, subject, module, cancelled, transcript, recording in connection.execute(
            QA_RECORDS, (date_from, date_to)).fetchall():
        out.append({"qa_ref": ref(session), "meeting_ref": ref(meeting),
                    "meeting_key": meeting_key.from_online_meeting_id(meeting),
                    "session_date": day.isoformat(), "subject": subject, "module": module,
                    "cancelled": str(cancelled or "").strip().lower() == "true",
                    "transcript_reference": bool(transcript), "recording_reference": bool(recording)})
    return out


def entry(record: dict, *, lms_keys: frozenset | None) -> dict:
    """What a preview / run stores of one QA record: references and facts, never an identifier.
    `lms_session_on_day` is None when the LMS could not be read."""
    lms = None if lms_keys is None or record["meeting_key"] is None else record["meeting_key"] in lms_keys
    return {**{k: v for k, v in record.items() if k != "meeting_key"},
            "parsable_meeting": record["meeting_key"] is not None, "lms_session_on_day": lms}


def registered(connection, date_from: date, date_to: date) -> list[dict]:
    """Every registry lecture of the range with its meeting reference (READ ONLY)."""
    return [{"lecture_id": lid, "session_date": day.isoformat(), "meeting_ref": ref(meeting), "suppressed": bool(s)}
            for lid, day, meeting, s in connection.execute(REGISTERED_MEETINGS, (date_from, date_to)).fetchall()]


def merge(records: list[dict], lectures: list[dict]) -> tuple[dict, list[dict]]:
    """
    Attach QA records to lectures by (Cairo date, meeting). Pure.

        records    QA entries (entry()), each with session_date and meeting_ref
        lectures   {"lecture_id", "session_date", "meeting_ref", "suppressed"} - registry and Teams, de-duplicated
                   by lecture_id by the caller

    Returns (lecture_id -> {"status": MATCHED | CONFLICT, "records": n, "qa_refs": [...], ...},
             QA-only groups: [{"session_date", "meeting_ref", "records": [entries]}]).
    """
    by_key: dict = defaultdict(list)
    for lecture in lectures:
        if lecture.get("meeting_ref"):
            by_key[(lecture["session_date"], lecture["meeting_ref"])].append(lecture)
    groups: dict = defaultdict(list)
    for record in records:
        # A record without a meeting can never be matched: it is its own group.
        groups[(record["session_date"], record["meeting_ref"] or f"qa:{record['qa_ref']}")].append(record)
    attached: dict = {}
    qa_only: list[dict] = []
    for (day, meeting), group in sorted(groups.items()):
        candidates = sorted(by_key.get((day, meeting), ()), key=lambda l: l["lecture_id"])
        live = [c for c in candidates if not c["suppressed"]]
        summary = {"records": len(group), "qa_refs": sorted(r["qa_ref"] for r in group),
                   "cancelled": all(r["cancelled"] for r in group),
                   "transcript_reference": any(r["transcript_reference"] for r in group),
                   "recording_reference": any(r["recording_reference"] for r in group)}
        if len(live) > 1:
            for lecture in live:                   # several lectures share the meeting that day: never guessed
                attached[lecture["lecture_id"]] = {"status": CONFLICT, "lectures_sharing_meeting": len(live),
                                                   **summary}
        elif live or candidates:
            attached[(live or candidates)[0]["lecture_id"]] = {"status": MATCHED, **summary}
        else:
            qa_only.append({"session_date": day, "meeting_ref": group[0]["meeting_ref"], "records": group})
    return attached, qa_only


def classify_qa_only(group: dict) -> tuple[str, str, str]:
    """(category, code, reason) for a QA-only lecture. Never eligible: it is not registered and not matched."""
    records = group["records"]
    if all(r["cancelled"] for r in records):
        return "SKIPPED", "QA_CANCELLED", "Cancelled in the QA record."
    if not any(r["parsable_meeting"] for r in records):
        return "REVIEW_REQUIRED", NO_MEETING, ("Only in the QA database, without a parsable Teams meeting: the "
                                               "lecture cannot be identified in Teams or the LMS.")
    lms = [r["lms_session_on_day"] for r in records if r["parsable_meeting"]]
    if any(v is None for v in lms):
        return "REVIEW_REQUIRED", LMS_UNCHECKED, ("Only in the QA database; the LMS could not be read for this day. "
                                                  "Preview again.")
    if any(lms):
        return "REVIEW_REQUIRED", SCHEDULE_UNKNOWN, (
            "Only in the QA database. The LMS has a session of this Teams meeting on this date, but the Teams "
            "calendar no longer lists the lecture, so its exact start - which the LMS match requires - is unknown. "
            "Not registered; an operator decision is needed.")
    return "MISSING_LMS_MATCH", NO_LMS_SESSION, (
        "Only in the QA database. The LMS has no session of this Teams meeting on this date, so there is "
        "nothing to publish it to.")


def qa_only_row(group: dict) -> dict:
    """A preview / run row for a QA-only lecture (no lecture_id: it is not a publisher lecture)."""
    records = group["records"]
    first = records[0]
    category, code, reason = classify_qa_only(group)
    lms = [r["lms_session_on_day"] for r in records]
    return {
        "lecture_id": None, "qa_ref": first["qa_ref"], "qa_refs": sorted(r["qa_ref"] for r in records),
        "subject": first["subject"], "module": first["module"], "origin": QA_ONLY, "sources": ["QA"],
        "provenance": "QA", "session_date": group["session_date"], "scheduled_start": None, "scheduled_end": None,
        "lms": {"status": "NO_LMS_SESSION" if code == NO_LMS_SESSION else
                ("SESSION_ON_DATE" if code == SCHEDULE_UNKNOWN else "NOT_CHECKED"), "occurrence_id": None},
        "recording": {"status": "NOT_CHECKED", "verified_count": 0, "candidates": 0, "selected_part": None,
                      "selection_rule": None, "selected_duration_seconds": None},
        "bunny": None, "release": None, "playback": None, "monitor_state": None,
        "verification": {"teams": "NOT_IN_TEAMS", "lms": "LIVE_DRY_RUN" if None not in lms else "NOT_CHECKED",
                         "recording": "NOT_CHECKED"},
        "basis": "QA_RECORD", "category": category, "proposed_action": "NONE", "reason": reason, "code": code,
        "missing_evidence": [] if category == "SKIPPED" else
        list(MISSING_EVIDENCE) + (["LMS_SESSION"] if code == NO_LMS_SESSION else []),
        "qa": {"status": QA_ONLY, "records": len(records), "cancelled": all(r["cancelled"] for r in records),
               "transcript_reference": any(r["transcript_reference"] for r in records),
               "recording_reference": any(r["recording_reference"] for r in records)},
        "data_source": "QA_DATABASE"}


def lms_keys_by_day(snapshot, days) -> dict:
    """Cairo date -> the meeting keys of the LMS's non-cancelled occurrences that day."""
    from app.common.time import cairo_business_date
    wanted = {d.isoformat() if isinstance(d, date) else d for d in days}
    out: dict = {d: set() for d in wanted}
    for occurrence in snapshot.occurrences:
        day = cairo_business_date(occurrence.scheduled_start).isoformat()
        if day in wanted and str(occurrence.status or "").lower() != "cancelled":
            out[day] |= set(occurrence.meeting_keys)
    return {d: frozenset(k) for d, k in out.items()}
