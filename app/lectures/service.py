import logging
import time
import uuid
from datetime import date

from app.common.hashing import lecture_identity
from app.common.errors import NO_ACTIVE_APTEM_GROUPS, PlatformError
from app.common.time import BUSINESS_DATE_RULE, CAIRO, business_date_trace, cairo_business_date
from app.graph.meetings import (
    NOT_ATTEMPTED,
    ORGANIZER_ID_UNAVAILABLE,
    RESOLVED,
)
from app.lectures.matching import match_active_group, normalize_group
from app.lectures.models import Lecture


class LectureDiscoveryService:
    """
    CANONICAL REGISTRY SCOPE
    ------------------------
    `public.lecture_sessions` is the canonical KBC LECTURE registry, not a
    mirror of the Teams calendar. A row exists only for a calendar occurrence
    that is

        * not cancelled,
        * carries a Teams JoinWebUrl, and
        * whose normalized subject exactly matches an active Aptem group.

    Every other calendar occurrence is audit evidence: it is counted and
    described in the discovery run's diagnostics and never becomes a row.
    """

    def __init__(self, *, calendar, meetings, aptem_repository, lecture_repository, run_repository, qa_validation_repository=None):
        self.calendar = calendar
        self.meetings = meetings
        self.aptem_repository = aptem_repository
        self.lecture_repository = lecture_repository
        self.run_repository = run_repository
        self.qa_validation_repository = qa_validation_repository
        self.log = logging.getLogger(__name__)

    def discover_day(self, kbc_connection, aptem_connection, target_date: date, *, persist: bool = True) -> dict:
        """
        Pipeline order:
            calendar discovery
            -> Teams event filtering
            -> exact active-Aptem subject matching      <- canonical boundary
            -> onlineMeeting resolution                 (candidates only)
            -> canonical registry persistence           (candidates only)

        Meeting resolution is the expensive, permission-sensitive step, so it
        runs only for occurrences that already matched an active Aptem group.
        A non-matching calendar event is never looked up and never persisted.
        """
        started = time.monotonic()
        run_id = self.run_repository.start(kbc_connection, target_date) if persist else uuid.uuid4()
        groups = self.aptem_repository.load_active_groups(aptem_connection)
        if not groups:
            raise PlatformError(
                NO_ACTIVE_APTEM_GROUPS,
                "correct Aptem source database returned zero active groups; discovery stopped",
            )
        batch = self.calendar.discover_calendar_events(target_date)
        lectures: list[Lecture] = []
        non_canonical: list[dict] = []
        created = updated = 0
        forbidden_contexts: list[dict] = []

        for event in batch.eligible_events:
            module = match_active_group(event.subject, groups)
            if module is None:
                # Audit evidence only. No Graph call, no registry row.
                non_canonical.append({
                    "subject": event.subject,
                    "normalized_subject": normalize_group(event.subject),
                    "exclusion_reason": "NOT_AN_ACTIVE_APTEM_GROUP",
                    "session_date": cairo_business_date(event.meeting_start).isoformat(),
                    "is_all_day": event.is_all_day,
                    "occurrence_type": event.occurrence_type,
                })
                continue

            resolution = self.meetings.resolve(event.join_url, event.calendar_organizer_address)
            if resolution.status == "FORBIDDEN_FOR_ORGANIZER":
                forbidden_contexts.append({
                    "subject": event.subject,
                    "meeting_lookup_context_source": resolution.meeting_lookup_context_source,
                    "reason": resolution.reason,
                })

            resolved = resolution.status == RESOLVED
            trace = business_date_trace(event.meeting_start)
            metadata: dict = {"business_date": trace}
            if resolution.reason:
                metadata["meeting_resolution_reason"] = resolution.reason
            if resolution.join_url_oid_hint_status:
                metadata["join_url_oid_hint_status"] = resolution.join_url_oid_hint_status
            if event.is_all_day:
                metadata["is_all_day"] = True
            if event.occurrence_type:
                metadata["occurrence_type"] = event.occurrence_type

            lecture = Lecture(
                lecture_id=lecture_identity(
                    calendar_user_upn=self.calendar.calendar_user_upn,
                    calendar_event_id=event.calendar_event_id,
                    i_cal_uid=event.i_cal_uid,
                ),
                calendar_user_upn=self.calendar.calendar_user_upn,
                calendar_event_id=event.calendar_event_id,
                i_cal_uid=event.i_cal_uid,
                meeting_id=resolution.meeting_id,
                join_url=event.join_url,
                subject=event.subject,
                normalized_subject=normalize_group(event.subject),
                module=module,
                scheduled_start=event.meeting_start,
                scheduled_end=event.meeting_end,
                session_date=cairo_business_date(event.meeting_start),
                calendar_timezone=event.calendar_timezone,
                # Cohort group mailbox: calendar metadata, never a user context.
                calendar_organizer_address=event.calendar_organizer_address,
                meeting_organizer_user_id=resolution.meeting_organizer_user_id,
                organizer_validation_status=resolution.organizer_validation or (
                    ORGANIZER_ID_UNAVAILABLE if resolved else NOT_ATTEMPTED
                ),
                meeting_lookup_user_id=resolution.meeting_lookup_user_id,
                meeting_lookup_context_source=resolution.meeting_lookup_context_source,
                join_url_oid_hint_status=resolution.join_url_oid_hint_status,
                graph_meeting_subject=resolution.subject,
                graph_meeting_start=resolution.start,
                graph_meeting_end=resolution.end,
                meeting_type=resolution.meeting_type,
                calendar_mapping_status=resolution.status,
                group_match_status="MATCHED",
                discovery_status="READY" if resolved else "REVIEW",
                is_cancelled=False,
                # An occurrence whose meeting never resolved is kept as a
                # distinct canonical lecture but is not downstream-eligible.
                downstream_ready=resolved and resolution.meeting_id is not None,
                metadata=metadata,
            )
            lectures.append(lecture)
            if persist:
                outcome = self.lecture_repository.upsert(kbc_connection, lecture)
                created += outcome == "created"
                updated += outcome == "updated"

        resolved_count = sum(item.meeting_id is not None for item in lectures)
        matched = len(lectures)
        status_counts: dict[str, int] = {}
        organizer_validation_counts: dict[str, int] = {}
        context_source_counts: dict[str, int] = {}
        oid_hint_counts: dict[str, int] = {}
        for item in lectures:
            status_counts[item.calendar_mapping_status] = status_counts.get(item.calendar_mapping_status, 0) + 1
            key = item.organizer_validation_status or NOT_ATTEMPTED
            organizer_validation_counts[key] = organizer_validation_counts.get(key, 0) + 1
            source = item.meeting_lookup_context_source or "NONE"
            context_source_counts[source] = context_source_counts.get(source, 0) + 1
            hint = item.join_url_oid_hint_status or "NONE"
            oid_hint_counts[hint] = oid_hint_counts.get(hint, 0) + 1

        unresolved_lectures = [
            {
                "lecture_id": str(item.lecture_id),
                "subject": item.subject,
                "scheduled_start": item.scheduled_start.isoformat(),
                "calendar_mapping_status": item.calendar_mapping_status,
                "meeting_resolution_reason": item.metadata.get("meeting_resolution_reason"),
                "downstream_ready": item.downstream_ready,
            }
            for item in lectures if not item.downstream_ready
        ]
        qa_comparison = None
        if self.qa_validation_repository is not None:
            qa_rows = self.qa_validation_repository.load_day(kbc_connection, target_date)
            qa_meetings = {row["meeting_id"] for row in qa_rows if row.get("meeting_id")}
            new_meetings = {item.meeting_id for item in lectures if item.meeting_id}
            qa_subjects = {normalize_group(row["subject"]) for row in qa_rows if row.get("subject")}
            new_subjects = {item.normalized_subject for item in lectures}
            qa_comparison = {
                "qa_sessions": len(qa_rows),
                "meeting_ids": {
                    "matched": sorted(qa_meetings & new_meetings),
                    "missing_from_new_discovery": sorted(qa_meetings - new_meetings),
                    "discovered_but_absent_from_qa": sorted(new_meetings - qa_meetings),
                },
                "normalized_subjects": {
                    "matched": sorted(qa_subjects & new_subjects),
                    "missing_from_new_discovery": sorted(qa_subjects - new_subjects),
                    "discovered_but_absent_from_qa": sorted(new_subjects - qa_subjects),
                },
            }
        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "mode": "SHADOW" if persist else "DRY_RUN",
            "calendar_events_found": batch.calendar_events_found,
            "teams_events_found": len(batch.eligible_events),
            "active_aptem_groups_loaded": len(groups),
            "aptem_matched_candidates": matched,
            "canonical_lecture_candidates": matched,
            "non_canonical_calendar_events": len(non_canonical),
            "meeting_lookups_attempted": matched,
            "online_meetings_resolved": resolved_count,
            "online_meetings_unresolved": matched - resolved_count,
            "downstream_ready_lectures": sum(item.downstream_ready for item in lectures),
            "meeting_status_counts": status_counts,
            "organizer_validation_counts": organizer_validation_counts,
            "meeting_context_source_counts": context_source_counts,
            "join_url_oid_hint_counts": oid_hint_counts,
            "forbidden_meeting_contexts": forbidden_contexts,
            "active_group_matches": matched,
            "unmatched_count": len(non_canonical), "error_count": 0,
            "registry_rows_written": matched if persist else 0,
            "registry_rows_created": created, "registry_rows_updated": updated,
            "total_registry_rows_for_date": self.lecture_repository.count_day(kbc_connection, target_date),
            "unresolved_lectures": unresolved_lectures,
            "non_canonical_calendar_event_details": non_canonical,
            "qa_comparison": qa_comparison,
            "status": "COMPLETED",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {
                "write_policy": "new_tables_only",
                "canonical_registry_rule": "ACTIVE_APTEM_MATCHED_TEAMS_OCCURRENCES_ONLY",
                "business_date_rule": BUSINESS_DATE_RULE,
                "registry_rows_created": created, "registry_rows_updated": updated,
                "qa_comparison": qa_comparison,
                "meeting_context_strategy": "DISCOVERY_MAILBOX_OBJECT_ID_PRIMARY",
                "meeting_status_counts": status_counts,
                "organizer_validation_counts": organizer_validation_counts,
                "meeting_context_source_counts": context_source_counts,
                "join_url_oid_hint_counts": oid_hint_counts,
                "non_canonical_calendar_events": non_canonical,
            },
        }
        if persist:
            self.run_repository.complete(kbc_connection, run_id, summary)
        self.log.info("lecture discovery completed", extra={"fields": {
            "service": "lecture_discovery", "operation": "discover_day", "run_id": str(run_id),
            "status": summary["status"], "duration_ms": summary["duration_ms"],
        }})
        return summary


def select_single_lecture(rows: list[dict], start: str | None = None) -> dict:
    candidates = rows
    if start:
        candidates = [row for row in rows if row["scheduled_start"].astimezone(CAIRO).strftime("%H:%M") == start]
    if not candidates:
        raise ValueError("no matching lecture found")
    if len(candidates) > 1:
        raise ValueError("multiple lectures matched; provide --start HH:MM")
    return candidates[0]
