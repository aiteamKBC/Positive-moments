"""
Graph client, calendar gateway, and JoinWebUrl handling.

Meeting user context, the optional JoinWebUrl Oid hint, and organizer identity
validation are covered in tests/unit/test_meeting_context.py.
"""
import json
from datetime import date
from unittest.mock import patch

from app.graph.calendar import CalendarGateway
from app.graph.client import Phase1GraphClient
from app.graph.meetings import canonicalize_join_url, organizer_oid_from_join_url
from automation.lecture_parts.graph_client import GraphAppClient, GraphResponse


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.paths = []
        self.headers = None

    def get_collection(self, path, *, headers=None):
        self.paths.append(path)
        self.headers = headers
        return self.rows

    def get_json(self, path, *, headers=None):
        self.user_path = path
        return {"id": "resolved-owner-id"}


def test_graph_token_is_cached_in_memory():
    client = GraphAppClient(tenant_id="t", client_id="c", client_secret="s", scope="https://graph.microsoft.com/.default", base_url="https://graph.microsoft.com/v1.0")
    response = GraphResponse(json.dumps({"access_token": "token", "expires_in": 3600}).encode(), "application/json", 200)
    with patch.object(client, "_open", return_value=response) as request:
        assert client._access_token() == "token"
        assert client._access_token() == "token"
    assert request.call_count == 1


def test_phase1_client_sends_prefer_without_exposing_authorization_control():
    client = Phase1GraphClient(tenant_id="t", client_id="c", client_secret="s", scope="https://graph.microsoft.com/.default", base_url="https://graph.microsoft.com/v1.0")
    response = GraphResponse(b'{"value":[]}', "application/json", 200)
    with patch.object(client, "_access_token", return_value="private-token"), patch.object(client, "_open", return_value=response) as opened:
        client.get_collection("/users/owner/calendarView", headers={"Prefer": 'IdType="ImmutableId"'})
    request = opened.call_args.args[0]
    assert request.get_header("Prefer") == 'IdType="ImmutableId"'
    assert request.get_header("Authorization") == "Bearer private-token"


def test_calendar_filters_cancelled_and_non_teams_events():
    base = {"id": "1", "iCalUId": "ical-1", "subject": "Group", "isCancelled": False,
            "start": {"dateTime": "2026-09-04T09:00:00", "timeZone": "Africa/Cairo"},
            "end": {"dateTime": "2026-09-04T10:00:00", "timeZone": "Africa/Cairo"}}
    rows = [
        {**base, "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/a"}},
        {**base, "id": "2", "isCancelled": True, "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/b"}},
        {**base, "id": "3", "onlineMeeting": None},
    ]
    result = CalendarGateway(FakeClient(rows), "calendar@example.invalid").discover_calendar_events(date(2026, 9, 4))
    assert result.calendar_events_found == 3
    assert [event.calendar_event_id for event in result.eligible_events] == ["1"]


def test_calendar_requests_outlook_immutable_event_ids():
    client = FakeClient([])
    CalendarGateway(client, "calendar@example.invalid").discover_calendar_events(date(2026, 9, 4))
    assert client.headers == {"Prefer": 'IdType="ImmutableId"'}


def test_calendar_keeps_the_cohort_organizer_as_calendar_metadata():
    """The organizer is a Unified Group mailbox; it is recorded, never used as a user."""
    rows = [{
        "id": "1", "iCalUId": "ical-1", "subject": "Ray-Project Management Office (PMO)",
        "isCancelled": False, "isAllDay": False, "type": "occurrence",
        "start": {"dateTime": "2026-09-04T08:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-04T10:00:00", "timeZone": "UTC"},
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/a"},
        "organizer": {"emailAddress": {"name": "Project Management Office (PMO)",
                                       "address": "ProjectManagementOfficePMO@example.invalid"}},
    }]
    event = CalendarGateway(FakeClient(rows), "cal@example.invalid").discover_calendar_events(
        date(2026, 9, 4)).eligible_events[0]
    assert event.calendar_organizer_address == "ProjectManagementOfficePMO@example.invalid"
    assert event.occurrence_type == "occurrence"
    assert event.is_all_day is False


def test_calendar_records_all_day_occurrences_for_business_date_audits():
    rows = [{
        "id": "1", "iCalUId": "ical-1", "subject": "Ai team", "isCancelled": False,
        "isAllDay": True, "type": "occurrence",
        "start": {"dateTime": "2026-09-03T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-04T00:00:00.0000000", "timeZone": "UTC"},
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/a"},
    }]
    event = CalendarGateway(FakeClient(rows), "cal@example.invalid").discover_calendar_events(
        date(2026, 9, 4)).eligible_events[0]
    assert event.is_all_day is True
    assert event.meeting_start.isoformat() == "2026-09-03T00:00:00+00:00"


def test_join_url_canonicalization():
    left = "HTTPS://Teams.Microsoft.com:443/l/meetup-join/abc/?b=2&a=1#ignored"
    right = "https://teams.microsoft.com/l/meetup-join/abc?a=1&b=2"
    assert canonicalize_join_url(left) == canonicalize_join_url(right)


def test_organizer_oid_is_extracted_from_the_join_url_context():
    url = ("https://teams.microsoft.com/l/meetup-join/19%3aabc%40thread.tacv2/123"
           "?context=%7b%22Tid%22%3a%22tenant-guid%22%2c%22Oid%22%3a%22organizer-guid%22%7d")
    assert organizer_oid_from_join_url(url) == "organizer-guid"


def test_join_url_without_context_has_no_oid():
    assert organizer_oid_from_join_url("https://teams.microsoft.com/l/meetup-join/19%3aabc") is None
    assert organizer_oid_from_join_url("https://teams.microsoft.com/x?context=not-json") is None
