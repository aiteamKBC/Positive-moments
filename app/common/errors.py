class PlatformError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


GRAPH_AUTH_ERROR = "graph_auth_error"
GRAPH_PERMISSION_ERROR = "graph_permission_error"
CALENDAR_QUERY_ERROR = "calendar_query_error"
ONLINE_MEETING_NOT_FOUND = "online_meeting_not_found"
ONLINE_MEETING_AMBIGUOUS = "online_meeting_ambiguous"
ONLINE_MEETING_QUERY_ERROR = "online_meeting_query_error"
INVALID_CALENDAR_EVENT = "invalid_calendar_event"
ACTIVE_GROUP_QUERY_ERROR = "active_group_query_error"
NO_ACTIVE_APTEM_GROUPS = "no_active_aptem_groups"
GROUP_NOT_MATCHED = "group_not_matched"
DATABASE_ERROR = "database_error"

# Phase 2A: raw transcript acquisition.
TRANSCRIPT_LIST_ERROR = "transcript_list_error"
TRANSCRIPT_CONTENT_ERROR = "transcript_content_error"

# Phase 3C2: the human-safe canary guard on the legacy writer CLI.
WRITER_GUARD_REFUSED = "writer_guard_refused"
