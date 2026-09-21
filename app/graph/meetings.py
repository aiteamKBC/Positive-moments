"""
Resolve a calendar JoinWebUrl to exactly one Microsoft Teams onlineMeeting.

MEETING USER CONTEXT POLICY
---------------------------
`/users/{userId}/onlineMeetings` needs a *user* object ID. Choosing that user
is the whole difficulty, because a KBC class calendar event's `organizer` is a
Microsoft 365 Unified Group (Teams cohort) mailbox, which is not a user object
and cannot host an onlineMeetings query.

PRIMARY (the production dependency):
    The configured discovery mailbox's Graph object ID, resolved once from
    KBC_LECTURE_CALENDAR_USER_UPN via /users/{upn}?$select=id.

SECONDARY (diagnostic hint, never the only dependency):
    The `Oid` Teams embeds in the JoinWebUrl `context` parameter. Microsoft
    documents the joinWebUrl format as internal and subject to change, and
    tells clients not to rely on information extracted from it. So the Oid is
    recorded as a hint, used to explain a mismatch, and used only as a GUARDED
    FALLBACK: tried solely when the primary context found no meeting and the
    hint names a different user. Discovery works unchanged when no Oid exists.
"""
import json
import urllib.parse

from app.common.errors import ONLINE_MEETING_QUERY_ERROR
from app.common.time import parse_graph_datetime
from app.graph.errors import translate_graph_error
from app.lectures.models import MeetingResolution
from app.graph.transport import GraphError


# Meeting resolution outcomes.
RESOLVED = "RESOLVED"
ONLINE_MEETING_NOT_FOUND = "ONLINE_MEETING_NOT_FOUND"
AMBIGUOUS_ONLINE_MEETING = "AMBIGUOUS_ONLINE_MEETING"
ORGANIZER_NOT_RESOLVABLE = "ORGANIZER_NOT_RESOLVABLE"
FORBIDDEN_FOR_ORGANIZER = "FORBIDDEN_FOR_ORGANIZER"
NOT_ATTEMPTED = "NOT_ATTEMPTED"

# Organizer identity validation, performed on the resolved onlineMeeting's
# participants.organizer.identity.user.id. It compares USER IDS, and has
# nothing to do with the cohort group address on the calendar event.
ORGANIZER_ID_CONFIRMED = "ORGANIZER_ID_CONFIRMED"
ORGANIZER_ID_DIFFERENT = "ORGANIZER_ID_DIFFERENT"
ORGANIZER_ID_UNAVAILABLE = "ORGANIZER_ID_UNAVAILABLE"

# Which user context the final lookup used.
DISCOVERY_MAILBOX_CONTEXT = "DISCOVERY_MAILBOX_OBJECT_ID"
JOIN_URL_OID_FALLBACK_CONTEXT = "JOIN_URL_OID_FALLBACK"

# What the optional JoinWebUrl Oid hint said about the primary context.
JOIN_URL_OID_ABSENT = "JOIN_URL_OID_ABSENT"
JOIN_URL_OID_MATCHES_CONTEXT = "JOIN_URL_OID_MATCHES_CONTEXT"
JOIN_URL_OID_DIFFERS_FROM_CONTEXT = "JOIN_URL_OID_DIFFERS_FROM_CONTEXT"


def organizer_oid_from_join_url(value: str) -> str | None:
    """
    Best-effort read of the `Oid` Teams puts in a JoinWebUrl `context` blob.

    DIAGNOSTIC ONLY. Microsoft documents this format as internal, so a None
    return is an ordinary outcome and never blocks resolution. See the module
    docstring for the production context strategy.
    """
    try:
        query = urllib.parse.urlsplit(value).query
    except ValueError:
        return None
    context = urllib.parse.parse_qs(query).get("context", [None])[0]
    if not context:
        return None
    try:
        oid = json.loads(context).get("Oid")
    except (json.JSONDecodeError, AttributeError):
        return None
    return oid if isinstance(oid, str) and oid.strip() else None


def canonicalize_join_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid JoinWebUrl")
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host if port is None or (scheme, port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%:@-._~!$&'()*+,;=")
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)))
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


class _MeetingLookupDenied(Exception):
    """A per-context 403; reported by the gateway, never raised out of it."""

    def __init__(self, reason: str):
        self.reason = reason


class OnlineMeetingGateway:
    def __init__(self, client, calendar_user_upn: str, *, allow_join_url_oid_fallback: bool = True):
        self.client = client
        self.calendar_user_upn = calendar_user_upn
        self.allow_join_url_oid_fallback = allow_join_url_oid_fallback
        self._user_ids: dict[str, str] = {}
        self._mailbox_object_id: str | None = None

    def resolve_user_object_id(self, user_upn: str) -> str | None:
        """
        UPN/SMTP address -> Graph object ID, or None when the directory has no
        such user. Group mailboxes are not users and resolve to None here.
        """
        if not user_upn or not user_upn.strip():
            return None
        cache_key = user_upn.casefold()
        if cache_key in self._user_ids:
            return self._user_ids[cache_key]
        owner = urllib.parse.quote(user_upn, safe="")
        try:
            payload = self.client.get_json(f"/users/{owner}?%24select=id")
        except GraphError as exc:
            if exc.status not in (404, 400):
                raise translate_graph_error(
                    exc, ONLINE_MEETING_QUERY_ERROR, "user context resolution"
                ) from exc
            # Guest/alias addresses are not addressable by path; fall back to a
            # filtered lookup on mail or userPrincipalName.
            escaped = user_upn.replace("'", "''")
            query = urllib.parse.urlencode(
                {
                    "$filter": f"mail eq '{escaped}' or userPrincipalName eq '{escaped}'",
                    "$select": "id,mail,userPrincipalName",
                },
                quote_via=urllib.parse.quote,
            )
            try:
                candidates = self.client.get_collection(f"/users?{query}")
            except GraphError as lookup_exc:
                raise translate_graph_error(
                    lookup_exc, ONLINE_MEETING_QUERY_ERROR, "user context lookup"
                ) from lookup_exc
            exact = [
                row for row in candidates
                if user_upn.casefold() in {
                    str(row.get("mail") or "").casefold(),
                    str(row.get("userPrincipalName") or "").casefold(),
                }
            ]
            if not exact:
                return None
            if len(exact) > 1:
                raise ValueError("multiple Graph users matched the meeting context address")
            payload = exact[0]
        user_id = payload.get("id")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("Graph user response did not contain an id")
        self._user_ids[cache_key] = user_id
        return user_id

    def discovery_mailbox_object_id(self) -> str | None:
        """The PRIMARY meeting user context. Resolved once, then cached."""
        if self._mailbox_object_id is None:
            self._mailbox_object_id = self.resolve_user_object_id(self.calendar_user_upn)
        return self._mailbox_object_id

    def _query_meetings(self, user_id: str, join_url: str) -> list[dict]:
        """
        Exactly-matching onlineMeetings within one user context.

        Graph answers an unmatched JoinWebUrl filter with 404 "meeting
        properties are not found" rather than an empty collection, so a 404
        here means NO SUCH MEETING, not a missing user: the user context was
        already resolved from the directory before this call.
        """
        escaped = join_url.replace("'", "''")
        query = urllib.parse.urlencode(
            {"$filter": f"JoinWebUrl eq '{escaped}'"},
            quote_via=urllib.parse.quote,
        )
        owner = urllib.parse.quote(user_id, safe="")
        try:
            rows = self.client.get_collection(f"/users/{owner}/onlineMeetings?{query}")
        except GraphError as exc:
            if exc.status == 404:
                return []
            if exc.status == 403:
                translated = translate_graph_error(exc, ONLINE_MEETING_QUERY_ERROR, "online meeting query")
                raise _MeetingLookupDenied(
                    getattr(translated, "diagnostic_reason", None) or "GRAPH_ACCESS_FORBIDDEN"
                ) from exc
            raise translate_graph_error(exc, ONLINE_MEETING_QUERY_ERROR, "online meeting query") from exc
        wanted = canonicalize_join_url(join_url)
        return [
            row for row in rows
            if isinstance(row.get("joinWebUrl"), str)
            and canonicalize_join_url(row["joinWebUrl"]) == wanted
        ]

    @staticmethod
    def _validate_organizer_identity(row: dict, context_user_id: str) -> tuple[str | None, str]:
        """
        Compare the meeting's own organizer USER ID to the context we queried.

        The calendar event's organizer is a cohort group mailbox and is
        deliberately not part of this comparison.
        """
        organizer = (((row.get("participants") or {}).get("organizer") or {}).get("identity") or {}).get("user") or {}
        organizer_id = organizer.get("id")
        if not isinstance(organizer_id, str) or not organizer_id.strip():
            return None, ORGANIZER_ID_UNAVAILABLE
        if organizer_id.casefold() == context_user_id.casefold():
            return organizer_id, ORGANIZER_ID_CONFIRMED
        return organizer_id, ORGANIZER_ID_DIFFERENT

    def resolve(self, join_url: str, calendar_organizer_address: str | None = None) -> MeetingResolution:
        """
        Resolve one JoinWebUrl in the discovery mailbox's user context.

        `calendar_organizer_address` is accepted for calendar provenance only.
        It is never used as a meeting user context: in this tenant it is a
        Unified Group cohort mailbox, not a user.
        """
        hint_oid = organizer_oid_from_join_url(join_url)
        try:
            primary = self.discovery_mailbox_object_id()
        except ValueError as exc:
            return MeetingResolution(ORGANIZER_NOT_RESOLVABLE, reason=str(exc))
        if primary is None:
            return MeetingResolution(
                ORGANIZER_NOT_RESOLVABLE,
                reason="DISCOVERY_MAILBOX_NOT_IN_DIRECTORY",
                join_url_oid_hint=hint_oid,
                join_url_oid_hint_status=JOIN_URL_OID_ABSENT if hint_oid is None
                else JOIN_URL_OID_DIFFERS_FROM_CONTEXT,
            )

        if hint_oid is None:
            hint_status = JOIN_URL_OID_ABSENT
        elif hint_oid.casefold() == primary.casefold():
            hint_status = JOIN_URL_OID_MATCHES_CONTEXT
        else:
            hint_status = JOIN_URL_OID_DIFFERS_FROM_CONTEXT

        context_source = DISCOVERY_MAILBOX_CONTEXT
        user_id = primary
        try:
            exact = self._query_meetings(primary, join_url)
            # Guarded fallback: only when the primary context genuinely has no
            # such meeting AND the internal-format hint names a different user.
            if (
                not exact
                and self.allow_join_url_oid_fallback
                and hint_status == JOIN_URL_OID_DIFFERS_FROM_CONTEXT
            ):
                fallback = self._query_meetings(hint_oid, join_url)
                if fallback:
                    exact, user_id, context_source = fallback, hint_oid, JOIN_URL_OID_FALLBACK_CONTEXT
        except _MeetingLookupDenied as denied:
            return MeetingResolution(
                FORBIDDEN_FOR_ORGANIZER, reason=denied.reason,
                meeting_lookup_user_id=user_id,
                meeting_lookup_context_source=context_source,
                join_url_oid_hint=hint_oid, join_url_oid_hint_status=hint_status,
            )

        common = {
            "meeting_lookup_user_id": user_id,
            "meeting_lookup_context_source": context_source,
            "join_url_oid_hint": hint_oid,
            "join_url_oid_hint_status": hint_status,
        }
        if not exact:
            return MeetingResolution(
                ONLINE_MEETING_NOT_FOUND,
                reason="NO_EXACT_JOIN_URL_MATCH_IN_ANY_CONTEXT",
                **common,
            )
        if len(exact) > 1:
            return MeetingResolution(
                AMBIGUOUS_ONLINE_MEETING, reason="MULTIPLE_EXACT_JOIN_URL_MATCHES", **common
            )
        row = exact[0]
        organizer_id, validation = self._validate_organizer_identity(row, user_id)
        return MeetingResolution(
            status=RESOLVED,
            meeting_id=str(row["id"]) if row.get("id") else None,
            subject=row.get("subject"),
            start=parse_graph_datetime(row["startDateTime"], "UTC") if row.get("startDateTime") else None,
            end=parse_graph_datetime(row["endDateTime"], "UTC") if row.get("endDateTime") else None,
            meeting_type=row.get("meetingType"),
            meeting_organizer_user_id=organizer_id,
            organizer_validation=validation,
            **common,
        )
