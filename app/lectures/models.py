from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class CalendarEvent:
    calendar_event_id: str
    i_cal_uid: str | None
    subject: str
    meeting_start: datetime
    meeting_end: datetime
    calendar_timezone: str | None
    join_url: str
    is_cancelled: bool = False
    # The Microsoft 365 Unified Group (Teams cohort) mailbox that owns the
    # event. Calendar metadata only: it is NOT the Teams meeting organizer and
    # is never used as an onlineMeetings user context.
    calendar_organizer_address: str | None = None
    is_all_day: bool = False
    occurrence_type: str | None = None


@dataclass(frozen=True)
class CalendarDiscoveryBatch:
    calendar_events_found: int
    eligible_events: list[CalendarEvent]


@dataclass(frozen=True)
class MeetingResolution:
    status: str
    reason: str | None = None
    meeting_id: str | None = None
    subject: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    meeting_type: str | None = None
    # participants.organizer.identity.user.id on the resolved onlineMeeting.
    meeting_organizer_user_id: str | None = None
    organizer_validation: str | None = None
    # Provenance of the user context the lookup ran in.
    meeting_lookup_user_id: str | None = None
    meeting_lookup_context_source: str | None = None
    # Secondary, internal-format JoinWebUrl hint. Diagnostic only.
    join_url_oid_hint: str | None = None
    join_url_oid_hint_status: str | None = None


@dataclass
class Lecture:
    lecture_id: UUID
    calendar_user_upn: str
    calendar_event_id: str
    i_cal_uid: str | None
    meeting_id: str | None
    join_url: str
    subject: str
    normalized_subject: str
    module: str | None
    scheduled_start: datetime
    scheduled_end: datetime
    session_date: date
    calendar_timezone: str | None
    calendar_organizer_address: str | None
    meeting_organizer_user_id: str | None
    organizer_validation_status: str | None
    meeting_lookup_user_id: str | None
    meeting_lookup_context_source: str | None
    join_url_oid_hint_status: str | None
    graph_meeting_subject: str | None
    graph_meeting_start: datetime | None
    graph_meeting_end: datetime | None
    meeting_type: str | None
    calendar_mapping_status: str
    group_match_status: str
    discovery_status: str
    is_cancelled: bool
    downstream_ready: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return asdict(self)
