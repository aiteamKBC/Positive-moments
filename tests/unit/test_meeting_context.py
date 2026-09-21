"""
Meeting user context and organizer identity validation.

PRIMARY context is the configured discovery mailbox's Graph object ID.
The JoinWebUrl `Oid` is an internal, documented-as-unstable format: it is a
secondary diagnostic hint and an optional guarded fallback, never a required
production dependency.

Organizer validation compares the resolved meeting's
participants.organizer.identity.user.id to that user context. The calendar
event's organizer is a Unified Group cohort mailbox and is NOT part of it.
"""
import pytest

from app.common.errors import PlatformError
from app.graph.meetings import (
    AMBIGUOUS_ONLINE_MEETING,
    DISCOVERY_MAILBOX_CONTEXT,
    FORBIDDEN_FOR_ORGANIZER,
    JOIN_URL_OID_ABSENT,
    JOIN_URL_OID_DIFFERS_FROM_CONTEXT,
    JOIN_URL_OID_FALLBACK_CONTEXT,
    JOIN_URL_OID_MATCHES_CONTEXT,
    ONLINE_MEETING_NOT_FOUND,
    ORGANIZER_ID_CONFIRMED,
    ORGANIZER_ID_DIFFERENT,
    ORGANIZER_ID_UNAVAILABLE,
    ORGANIZER_NOT_RESOLVABLE,
    RESOLVED,
    OnlineMeetingGateway,
    organizer_oid_from_join_url,
)
from automation.lecture_parts.graph_client import GraphError


MAILBOX_ID = "mailbox-object-id"
PLAIN_URL = "https://teams.microsoft.com/l/meetup-join/19%3aabc%40thread.tacv2/1"


def _url_with_oid(oid):
    return PLAIN_URL + "?context=%7b%22Tid%22%3a%22tenant%22%2c%22Oid%22%3a%22" + oid + "%22%7d"


class MailboxClient:
    """Resolves the discovery mailbox UPN; serves meetings per user context."""

    def __init__(self, by_context=None):
        self.by_context = by_context or {}
        self.paths = []
        self.user_paths = []

    def get_json(self, path, *, headers=None):
        self.user_paths.append(path)
        return {"id": MAILBOX_ID}

    def get_collection(self, path, *, headers=None):
        self.paths.append(path)
        context = path.split("/users/")[1].split("/onlineMeetings")[0]
        result = self.by_context.get(context)
        if isinstance(result, GraphError):
            raise result
        return result or []


def _meeting(url, organizer_id=MAILBOX_ID):
    row = {"id": "meeting-1", "joinWebUrl": url, "subject": "Lecture"}
    if organizer_id is not None:
        row["participants"] = {"organizer": {"identity": {"user": {"id": organizer_id}}}}
    return row


def _contexts_for(url):
    return {MAILBOX_ID: [_meeting(url)]}


# --- primary context ---------------------------------------------------------


def test_discovery_mailbox_object_id_is_the_primary_meeting_context():
    url = _url_with_oid(MAILBOX_ID)
    client = MailboxClient(_contexts_for(url))
    gateway = OnlineMeetingGateway(client, "discovery@example.invalid")
    result = gateway.resolve(url, "Cohort@example.invalid")

    assert result.status == RESOLVED
    assert result.meeting_lookup_context_source == DISCOVERY_MAILBOX_CONTEXT
    assert result.meeting_lookup_user_id == MAILBOX_ID
    # The mailbox UPN was resolved to an object ID; the URL never carries a UPN.
    assert "discovery%40example.invalid" in client.user_paths[0]
    assert f"/users/{MAILBOX_ID}/onlineMeetings" in client.paths[0]
    assert "%40" not in client.paths[0].split("/onlineMeetings")[0]


def test_the_cohort_group_address_is_never_used_as_a_user_context():
    url = _url_with_oid(MAILBOX_ID)
    client = MailboxClient(_contexts_for(url))
    OnlineMeetingGateway(client, "discovery@example.invalid").resolve(
        url, "Ray-ManagingSuccessfulProgrammes@example.invalid"
    )
    assert all("Ray-Managing" not in path for path in client.user_paths + client.paths)


def test_mailbox_object_id_is_resolved_once_and_cached():
    url = _url_with_oid(MAILBOX_ID)
    client = MailboxClient(_contexts_for(url))
    gateway = OnlineMeetingGateway(client, "discovery@example.invalid")
    gateway.resolve(url)
    gateway.resolve(url)
    assert len(client.user_paths) == 1


def test_resolution_does_not_require_a_join_url_oid():
    """A join URL with no `context` blob at all still resolves normally."""
    client = MailboxClient(_contexts_for(PLAIN_URL))
    result = OnlineMeetingGateway(client, "discovery@example.invalid").resolve(PLAIN_URL)

    assert organizer_oid_from_join_url(PLAIN_URL) is None
    assert result.status == RESOLVED
    assert result.join_url_oid_hint_status == JOIN_URL_OID_ABSENT
    assert result.meeting_lookup_context_source == DISCOVERY_MAILBOX_CONTEXT


def test_unresolvable_discovery_mailbox_is_reported_not_raised():
    class NoMailbox(MailboxClient):
        def get_json(self, path, *, headers=None):
            raise GraphError("Microsoft Graph", 404, "Request_ResourceNotFound", "not found")

        def get_collection(self, path, *, headers=None):
            if path.startswith("/users?"):
                return []
            raise AssertionError("no meeting lookup without a user context")

    result = OnlineMeetingGateway(NoMailbox(), "gone@example.invalid").resolve(PLAIN_URL)
    assert result.status == ORGANIZER_NOT_RESOLVABLE
    assert result.reason == "DISCOVERY_MAILBOX_NOT_IN_DIRECTORY"


# --- optional Oid hint -------------------------------------------------------


def test_matching_oid_is_recorded_as_a_confirming_hint():
    url = _url_with_oid(MAILBOX_ID)
    result = OnlineMeetingGateway(MailboxClient(_contexts_for(url)), "d@example.invalid").resolve(url)
    assert result.join_url_oid_hint == MAILBOX_ID
    assert result.join_url_oid_hint_status == JOIN_URL_OID_MATCHES_CONTEXT


def test_differing_oid_is_only_a_hint_when_the_primary_context_resolves():
    url = _url_with_oid("someone-else-oid")
    client = MailboxClient(_contexts_for(url))
    result = OnlineMeetingGateway(client, "d@example.invalid").resolve(url)

    assert result.status == RESOLVED
    assert result.join_url_oid_hint_status == JOIN_URL_OID_DIFFERS_FROM_CONTEXT
    # The differing hint explains the data; it does not redirect the lookup.
    assert result.meeting_lookup_context_source == DISCOVERY_MAILBOX_CONTEXT
    # Only one lookup ran, and it ran as the mailbox. (The Oid still appears
    # inside the filtered join URL itself, so check the user segment alone.)
    contexts = [path.split("/users/")[1].split("/onlineMeetings")[0] for path in client.paths]
    assert contexts == [MAILBOX_ID]


def test_guarded_oid_fallback_runs_only_after_the_primary_finds_nothing():
    url = _url_with_oid("other-user-oid")
    client = MailboxClient({MAILBOX_ID: [], "other-user-oid": [_meeting(url, "other-user-oid")]})
    result = OnlineMeetingGateway(client, "d@example.invalid").resolve(url)

    assert result.status == RESOLVED
    assert result.meeting_lookup_context_source == JOIN_URL_OID_FALLBACK_CONTEXT
    assert result.meeting_lookup_user_id == "other-user-oid"
    # Primary was attempted first.
    assert f"/users/{MAILBOX_ID}/onlineMeetings" in client.paths[0]
    assert "/users/other-user-oid/onlineMeetings" in client.paths[1]


def test_the_oid_fallback_can_be_switched_off_entirely():
    url = _url_with_oid("other-user-oid")
    client = MailboxClient({MAILBOX_ID: [], "other-user-oid": [_meeting(url, "other-user-oid")]})
    result = OnlineMeetingGateway(
        client, "d@example.invalid", allow_join_url_oid_fallback=False
    ).resolve(url)

    assert result.status == ONLINE_MEETING_NOT_FOUND
    assert len(client.paths) == 1


# --- meeting outcomes --------------------------------------------------------


def test_graph_404_on_the_filter_means_no_meeting_not_a_missing_user():
    """Graph answers an unmatched JoinWebUrl filter with 404 3004, not an empty page."""
    url = _url_with_oid(MAILBOX_ID)
    client = MailboxClient({
        MAILBOX_ID: GraphError("Microsoft Graph", 404, "NotFound", "3004: Meeting properties are not found")
    })
    result = OnlineMeetingGateway(client, "d@example.invalid").resolve(url)

    assert result.status == ONLINE_MEETING_NOT_FOUND
    assert result.reason == "NO_EXACT_JOIN_URL_MATCH_IN_ANY_CONTEXT"
    # The user context itself resolved fine, so it must not be blamed.
    assert result.meeting_lookup_user_id == MAILBOX_ID


def test_exact_join_url_match_never_takes_the_first_row():
    wanted = "https://teams.microsoft.com/l/meetup-join/right?a=1&b=2"
    client = MailboxClient({MAILBOX_ID: [
        {"id": "wrong", "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/wrong"},
        {"id": "right", "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/right?b=2&a=1"},
    ]})
    result = OnlineMeetingGateway(client, "d@example.invalid").resolve(wanted)
    assert result.status == RESOLVED
    assert result.meeting_id == "right"


def test_ambiguous_match_is_distinct_from_not_found():
    url = PLAIN_URL
    duplicate = [{"id": "1", "joinWebUrl": url}, {"id": "2", "joinWebUrl": url}]
    assert OnlineMeetingGateway(MailboxClient({MAILBOX_ID: duplicate}), "d@x.invalid").resolve(url).status \
        == AMBIGUOUS_ONLINE_MEETING
    assert OnlineMeetingGateway(MailboxClient({MAILBOX_ID: []}), "d@x.invalid").resolve(url).status \
        == ONLINE_MEETING_NOT_FOUND


def test_forbidden_context_is_returned_with_provenance_not_raised():
    client = MailboxClient({
        MAILBOX_ID: GraphError("Microsoft Graph", 403, "Forbidden", "Application access policy not configured")
    })
    result = OnlineMeetingGateway(client, "d@example.invalid").resolve(PLAIN_URL)
    assert result.status == FORBIDDEN_FOR_ORGANIZER
    assert result.reason == "APPLICATION_ACCESS_POLICY_DENIED"
    assert result.meeting_lookup_user_id == MAILBOX_ID
    assert result.meeting_lookup_context_source == DISCOVERY_MAILBOX_CONTEXT


def test_non_permission_graph_errors_still_raise():
    client = MailboxClient({MAILBOX_ID: GraphError("Microsoft Graph", 500, "InternalServerError", "boom")})
    with pytest.raises(PlatformError):
        OnlineMeetingGateway(client, "d@example.invalid").resolve(PLAIN_URL)


# --- organizer identity validation -------------------------------------------


def test_organizer_id_confirmed_when_the_meeting_organizer_is_the_context_user():
    url = _url_with_oid(MAILBOX_ID)
    result = OnlineMeetingGateway(MailboxClient(_contexts_for(url)), "d@x.invalid").resolve(url)
    assert result.meeting_organizer_user_id == MAILBOX_ID
    assert result.organizer_validation == ORGANIZER_ID_CONFIRMED


def test_organizer_id_different_is_detected_not_silently_accepted():
    client = MailboxClient({MAILBOX_ID: [_meeting(PLAIN_URL, organizer_id="a-different-user")]})
    result = OnlineMeetingGateway(client, "d@x.invalid").resolve(PLAIN_URL)
    assert result.organizer_validation == ORGANIZER_ID_DIFFERENT
    assert result.meeting_organizer_user_id == "a-different-user"


def test_organizer_id_unavailable_when_graph_returns_no_organizer_identity():
    client = MailboxClient({MAILBOX_ID: [_meeting(PLAIN_URL, organizer_id=None)]})
    result = OnlineMeetingGateway(client, "d@x.invalid").resolve(PLAIN_URL)
    assert result.organizer_validation == ORGANIZER_ID_UNAVAILABLE
    assert result.meeting_organizer_user_id is None


def test_validation_ignores_the_calendar_cohort_organizer_entirely():
    """A cohort address that looks nothing like the meeting organizer is still CONFIRMED."""
    url = _url_with_oid(MAILBOX_ID)
    client = MailboxClient(_contexts_for(url))
    result = OnlineMeetingGateway(client, "d@x.invalid").resolve(
        url, "Group2-MarketingExecutiveLevel4-May25@kentbusinesscollege.invalid"
    )
    assert result.organizer_validation == ORGANIZER_ID_CONFIRMED
